import signal
import sys
import os
import time
from typing import Union
import platform 

from psutil import virtual_memory, cpu_count
import numpy as np
from torch.utils.data import DataLoader
import torch 
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn, TimeRemainingColumn
from transformers import TrainingArguments, Trainer
from tokenizers import Tokenizer
from torch_optimizer import Adafactor

# import accelerate
from accelerate import Accelerator
from accelerate.utils import set_seed

# import 自定义类和函数
from model.chat_model import TextToTextModel
from utils.logger import Logger
from model.dataset import MyDataset
from config import TrainConfig, T5ModelConfig
from utils.functions import (
    get_bleu4_score, 
    save_model_config, 
    get_free_space_of_disk, 
    my_average,
    get_path_of_suffix_files,
)


def transformers_trainer(config: TrainConfig) -> None:
    ''''
    
    '''
    trainer_args = TrainingArguments()

    model = None 
    train_data = None
    dev_data = None
    tokenizer = None 
    trainer = Trainer(
        model=model,
        args=trainer_args,
        train_dataset=train_data,
        eval_dataset=dev_data,
        tokenizer=tokenizer,
        compute_metrics=None,
    )

class ChatTrainer:
    def __init__(self, train_config: TrainConfig, model_config: T5ModelConfig, ) -> None:
        
        self.train_config = train_config
        self.model_config = model_config

        # file_name=None会自动生成以当前日期命名的log文件名
        self.logger = Logger('chat_trainer', std_out=True, save2file=True, file_name=None)

        self.model = None
        self.accelerator = None

        signal.signal(signal.SIGINT, self.process_exit_handler)

        self.is_win_platform = True if platform.system().lower() == 'windows' else False

        torch.manual_seed(train_config.seed)
        torch.cuda.manual_seed_all(train_config.seed)
    
    def process_exit_handler(self, signal_received, frame) -> None:
        '''
        进程退出时的操作，保存模型
        '''
        if self.accelerator and self.accelerator.is_main_process:
            ask = "are you sure to exit this process?  Yes (y) or No (n)"
            self.accelerator.print(ask)
            ins = input()
            
            if ins.lower() in ('yes', 'y'):

                suffix =  'exit_save_{}'.format(str(time.strftime('%Y%m%d%H%M%S', time.localtime())))
                self.save_model(suffix)

                self.accelerator.print('model ckeck point has been saved!')
                sys.exit(0)
            else:
                print('do nothing!')
        else:
            print('process not in trainingg, exit.')
            sys.exit(0)

    def save_model(self, suffix: Union[str, int]) -> None:
        '''保存模型到文件
        '''
        if self.model and self.accelerator:
            self.accelerator.wait_for_everyone()
            unwrap_model = self.accelerator.unwrap_model(self.model)
            model_dict =  self.accelerator.get_state_dict(unwrap_model)
            torch.save(model_dict, self.train_config.model_file.format(suffix))

    
    def delete_early_checkpoint(self, epoch: int, keep_latest_n: int=3,) -> None:
        '''
        删除最早的模型，最保留最近keep_latest_n个模型文件
        '''
        model_save_path = self.train_config.model_file
        model_save_path = model_save_path.replace('\\', '/')    # 针对win的路径，将\替换为/
        model_save_path = '/'.join(model_save_path.split('/')[0: -1])   # 删除末尾文件名后缀
        
        model_files = get_path_of_suffix_files(model_save_path, suffix='.pth', with_create_time=True)
        
        # 进程异常退出保存模型文件不在删除范围
        train_save_model_fils = []
        for item in model_files:
            if 'exit_save' not in item[0]:

                # 大于当前epoch的文件不不删除
                f_epoch = int(item[0].split('.')[-2])
                if epoch >= f_epoch:
                    print(epoch, f_epoch, item)
                    train_save_model_fils.append(item)

        train_save_model_fils.sort(key=lambda x: x[1])  # 按照时间从小到大排序

        if len(train_save_model_fils) <= keep_latest_n:
            return
        
        to_delete_files = train_save_model_fils[0: -keep_latest_n]
        for item in to_delete_files:
            os.remove(item[0])

    
    def train(self, ) -> None:
        '''
        '''
        log = self.logger
        train_config = self.train_config
        model_config = self.model_config

        # 根据剩余内存大小决定是否完全加载数据集到内存中
        unuse_mem = virtual_memory().available / (1024 ** 3)  # 单位：GB
        unuse_disk = get_free_space_of_disk('./')

        # 剩余内存≥32GB将把数据集留在内存中
        keep_in_memory = True if unuse_mem >= 32.0 else False

        log.info('cpu memory available: {:.2f} GB, disk space available: {:.2f} GB, keep dataset in memory: {}.'\
                 .format(unuse_mem, unuse_disk, keep_in_memory), save_to_file=True)
        log.info('loading datasets ...')

        # args for dataloader
        pin_memory = False if self.is_win_platform else True
        num_workers = 0
        if not self.is_win_platform:
            cpu_cnt = cpu_count(logical=False)
            if cpu_cnt >= 16:
                num_workers = 8
            else:
                num_workers = int(cpu_cnt // 2)

        train_dataset = MyDataset(
            parquet_file=train_config.train_file,
            tokenizer_file=train_config.tokenizer_file,
            keep_in_memory=keep_in_memory,
            max_seq_len=train_config.max_seq_len,
        )
        valid_dataset = MyDataset(
            parquet_file=train_config.validation_file,
            tokenizer_file=train_config.tokenizer_file,
            keep_in_memory=keep_in_memory,
            max_seq_len=train_config.max_seq_len,
        )

        batch_size = train_config.batch_size_per_gpu

        train_dataloader = DataLoader(
            train_dataset, 
            batch_size=batch_size,  
            shuffle=True,
            collate_fn=train_dataset.collate_fn,
            pin_memory=pin_memory,
            num_workers=num_workers,
        )
        valid_dataloader = DataLoader(
            valid_dataset, 
            batch_size=batch_size, 
            shuffle=False,
            collate_fn=valid_dataset.collate_fn,
            pin_memory=pin_memory,
            num_workers=num_workers,
        )

        log.info('train dataset size: {}, validation dataset size: {}, pin_memory: {},  num_workers: {}.'\
                .format(len(train_dataset), len(valid_dataset), pin_memory, num_workers), save_to_file=True)
        
        # 梯度累计的步数
        accumulation_steps = train_config.gradient_accumulation_steps

        set_seed(train_config.seed)

        accelerator = Accelerator(
            mixed_precision=train_config.mixed_precision,       # 混合精度
            gradient_accumulation_steps=accumulation_steps,     # 梯度累积
        )

        device = accelerator.device
        log.info('using device: {} '.format(str(device)), save_to_file=True)

        # T5: All labels set to `-100` are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        tokenizer = train_dataset.tokenizer
        decoder_start_token_id = tokenizer.token_to_id('[PAD]')
        model_config.vocab_size = tokenizer.get_vocab_size()  # 往config添加vocab_size

        model = TextToTextModel(config=model_config, decoder_start_token_id=decoder_start_token_id)

        # 保存模型配置，方便修改配置后恢复
        save_model_config(model.t5_config.to_diff_dict(), train_config.model_config_file)
        
        # T5训练，论文推荐使用Adafactor
        optimizer = Adafactor(params=model.parameters(), lr=train_config.learn_rate)


        
        # 获取当前机器有多少个GPU，默认全部使用
        num_gpus_used = accelerator.state.num_processes

        # 单机多卡，每个step总共的batch_size = batch_size_per_gpu * num_gpus_used
        # total_batch_size 初始化为batch_size_per_gpu真的只有CPU的情况
        total_batch_size = train_config.batch_size_per_gpu
        if num_gpus_used >= 1:
            total_batch_size = num_gpus_used * train_config.batch_size_per_gpu

        steps_per_epoch = int(np.ceil(len(train_dataset) // total_batch_size))
        eval_steps = int(np.ceil(len(valid_dataset) // total_batch_size))

        
        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
                optimizer=optimizer, 
                max_lr=train_config.div_factor * train_config.learn_rate, 
                epochs=train_config.epochs, 
                steps_per_epoch=int(np.ceil( len(train_dataset) / (batch_size * accumulation_steps) )),  # 梯度累积相当于增大了batch_size
                div_factor=train_config.div_factor,
                cycle_momentum=False,
            )
        
        model, optimizer, lr_scheduler, train_dataloader, valid_dataloader = accelerator.prepare(
                model, 
                optimizer,
                lr_scheduler, 
                train_dataloader, 
                valid_dataloader,
            )
        
        self.model = model
        self.accelerator = accelerator
        
        best_bleu4 = 0.0
        best_epoch = 0
        epoch_loss_list = []

        log_loss_interval_n = 50 # 每间隔 n 步保存一次loss到文件

        # 添加进度条，只在主进程更新
        if accelerator.is_main_process:
            progress = Progress(TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                TextColumn("[bold blue]{task.fields[show_info]}"),
                refresh_per_second=0.2,  # 每5秒钟更新一次，不要频繁更新
                )
            
            epoch_progress = progress.add_task(description='epoch: ', show_info='', total=train_config.epochs)
            steps_progress = progress.add_task(description='steps: ', show_info='', \
                                                total=np.ceil(steps_per_epoch / log_loss_interval_n))
            eval_progress = progress.add_task(description='evaluate: ', show_info='', total=eval_steps, visible=False)

            self.progress = progress
            self.eval_progress = eval_progress

            progress.start()

        # end if

        for epoch in range(train_config.epochs):
            
            if accelerator.is_main_process:
                epoch_show_txt = 'epoch: {}/{}, avg_loss: {:.6f}, best_epoch: {}, best_bleu: {}'.format(
                    epoch, train_config.epochs, my_average(epoch_loss_list), best_epoch, best_bleu4
                )
                progress.update(epoch_progress, show_info=epoch_show_txt)
                progress.reset(steps_progress)

            epoch_loss_list = []
            model.train()
            
            for step, batch_data in enumerate(train_dataloader):

                input_ids, input_mask = batch_data['input_ids'], batch_data['input_mask']
                target_ids = batch_data['target_ids']

                # for t5 model, all labels set to `-100` are ignored (masked)
                target_ids[target_ids == decoder_start_token_id] = -100

                outputs = model(
                    input_ids=input_ids,
                    input_mask=input_mask,
                    labels=target_ids
                )

                loss = outputs.loss.mean() / accumulation_steps

                # attention here! loss.backward()
                accelerator.backward(loss) 

                # 梯度累计
                if step % accumulation_steps == 0:
                    accelerator.clip_grad_norm_(model.parameters(), 1.0)
                
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()
                
                # ==================================以下记录loss到日志============================================

                # 每n步更新一次，避免频繁的cpu-gpu数据复制
                # 参考：https://pytorch.org/tutorials/recipes/recipes/tuning_guide.html#avoid-unnecessary-cpu-gpu-synchronization
                if step % log_loss_interval_n == 0 or step == steps_per_epoch:

                    loss_cpu = outputs.loss.mean().detach().cpu().numpy()
                    epoch_loss_list.append(loss_cpu)
                    
                    info_txt = 'training loss: epoch:{}, step:{}, loss:{}, device:{}'.\
                        format(epoch, step, loss_cpu, str(accelerator.device))
                    
                    log.info(info_txt, std_out=False, save_to_file=True) # 保存 loss 到文件

                    # 更新进度条
                    if accelerator.is_main_process:
                        step_show_txt = 'step: {}/{}, loss: {:.6f}'.format(step, steps_per_epoch, loss_cpu)
                        progress.advance(steps_progress, advance=1)
                        progress.update(steps_progress, show_info=step_show_txt)

                # ==================================以上记录loss到日志============================================
                
                # if step >= 20:break
            
            #  end for batch setps
            
            if accelerator.is_main_process: 
                progress.advance(epoch_progress, advance=1)
                self.save_model(epoch)

            model.eval()
            
            cur_bleu4_score = self.evaluate(
                model=model,
                tokenizer=tokenizer,
                valid_dataloader=valid_dataloader,
                accelerator=accelerator,
                eval_steps=eval_steps,
                )

            # save model
            if cur_bleu4_score >= best_bleu4:
                best_bleu4 = cur_bleu4_score
                best_epoch = epoch            
            
                accelerator.wait_for_everyone()
                
                if accelerator.is_main_process: 
                    # 最多保存最近keep_latest_n_ckp个模型文件
                    # self.delete_early_checkpoint(epoch=epoch, keep_latest_n=train_config.keep_latest_n_ckp)
                    self.save_model(epoch)
                    

            # 每个epoch打印一下日志
            if accelerator.is_main_process:
                info_txt = 'epoch log: epoch:{}, avg_loss:{}, cur_bleu4:{}, best_bleu4:{}, best_epoch:{}'.\
                            format(epoch, my_average(epoch_loss_list), cur_bleu4_score, best_bleu4, best_epoch)
                # log.info(info_txt, std_out=True, save_to_file=True)
                self.print_and_log(info_txt, accelerator)
                epoch_loss_list = []


    def evaluate(self, 
                model: TextToTextModel, 
                tokenizer: Tokenizer,
                valid_dataloader: DataLoader, 
                accelerator: Accelerator,
                eval_steps: int,
            ) -> float:
        
        '''
        评估，返回平均的bleu分数
        '''
        max_seq_len = self.train_config.max_seq_len
        decode_batch = tokenizer.decode_batch
        bleu4_scores = []

        if accelerator.is_main_process:
            self.progress.reset(self.eval_progress)
            self.progress.update(self.eval_progress, visible=True)

        with torch.no_grad():
            for step, batch_data in enumerate(valid_dataloader):
                
                if accelerator.is_main_process:
                    self.progress.advance(self.eval_progress, advance=1)
                    self.progress.update(self.eval_progress, show_info='step: {}/{}'.format(step, eval_steps))

                input_ids, input_mask = batch_data['input_ids'], batch_data['input_mask']
                target_ids = batch_data['target_ids']

                outputs = accelerator.unwrap_model(model).generate(
                    input_ids=input_ids,
                    attention_mask=input_mask,
                    max_seq_len=max_seq_len,
                )

                # gather data from multi-gpus (used when in ddp mode)
                outputs = accelerator.gather_for_metrics(outputs).cpu().numpy()
                target_ids = accelerator.gather_for_metrics(target_ids).cpu().numpy()
        
                outputs = decode_batch(outputs,  skip_special_tokens=True)
                target_ids = decode_batch(target_ids, skip_special_tokens=True )


                # 删除decode出来字符间的空格
                outputs = [sentance.replace(' ', '') for sentance in outputs]
                target_ids = [sentance.replace(' ', '') for sentance in target_ids]

                # print(outputs, target_ids)

                bleu4_scores = [get_bleu4_score(reference=target_ids[i], outputs=outputs[i]) for i in range(len(target_ids))]
                bleu4_scores.extend(bleu4_scores)

                # if step >= 5: break
        
        avg_bleu4_score = my_average(bleu4_scores)
        if accelerator.is_main_process:
            self.progress.update(self.eval_progress, show_info='bleu4 score: {}'.format(avg_bleu4_score))
            self.progress.update(self.eval_progress, visible=False)

        return avg_bleu4_score

    def test(self, best_epoch: int=0) -> None:
        '''
        '''
        train_config = self.train_config
        model_config = self.model_config
        log = self.logger

        # args for dataloader
        pin_memory = False if self.is_win_platform else True
        num_workers = 0 if self.is_win_platform else 1

        test_dataset = MyDataset(
            parquet_file=train_config.train_file,
            tokenizer_file=train_config.tokenizer_file,
            keep_in_memory=True,
            max_seq_len=train_config.max_seq_len,
        )
        
        test_dataloader = DataLoader(
            test_dataset, 
            batch_size=train_config.batch_size_per_gpu,
            shuffle=False,
            collate_fn=test_dataset.collate_fn,
            pin_memory=pin_memory,
            num_workers=num_workers,
        )

        log.info('test dataset size: {}.'.format(len(test_dataset)), save_to_file=True)

        set_seed(train_config.seed)
        accelerator = Accelerator(mixed_precision=train_config.mixed_precision)
        device = accelerator.device
        log.info('using device: {} '.format(str(device)), save_to_file=True)

         # 获取当前运行使用了多少个GPU
        num_gpus_used = accelerator.state.num_processes

        # 单机多卡，每个step总共的batch_size = batch_size_per_gpu * num_gpus_used
        # total_batch_size 初始化为batch_size_per_gpu真的只有CPU的情况
        total_batch_size = train_config.batch_size_per_gpu
        if num_gpus_used >= 1:
            total_batch_size = num_gpus_used * train_config.batch_size_per_gpu

        # T5: All labels set to `-100` are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        tokenizer = test_dataset.tokenizer
        decoder_start_token_id = tokenizer.token_to_id('[PAD]')
        model_config.vocab_size = tokenizer.get_vocab_size()  # 往config添加vocab_size

        model = TextToTextModel(config=model_config, decoder_start_token_id=decoder_start_token_id)
        model.load_state_dict(torch.load(train_config.model_file.format(best_epoch)))
       
        model, test_dataloader = accelerator.prepare(
                model, 
                test_dataloader,
            )
        
        steps = int(np.ceil(len(test_dataset) // total_batch_size))

        bleu4 = 0.0
        bleu4_scores = []
        decode_batch = tokenizer.decode_batch
        max_seq_len = self.train_config.max_seq_len
        model.eval()

        if accelerator.is_main_process:
            progress = Progress(TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                TextColumn("[bold blue]{task.fields[show_info]}"),
                refresh_per_second=1.0,
                )
                
            steps_progress = progress.add_task(description='steps: ', show_info='', total=steps)
            progress.start()
            
        with torch.no_grad():
            for step, batch_data in enumerate(test_dataloader):

                if accelerator.is_main_process:
                    progress.advance(steps_progress, advance=1)
                    progress.update(steps_progress, show_info='step: {}/{}'.format(step, steps))

                input_ids, input_mask = batch_data['input_ids'], batch_data['input_mask']
                target_ids = batch_data['target_ids']

                # s = time.time()
                outputs = accelerator.unwrap_model(model).generate(
                    input_ids=input_ids,
                    attention_mask=input_mask,
                    max_seq_len=max_seq_len,
                )
                # accelerator.print('generate used: {}'.format(time.time() - s))

                # gather data from multi-gpus (used when in ddp mode)
                outputs = accelerator.gather_for_metrics(outputs).cpu().numpy()
                target_ids = accelerator.gather_for_metrics(target_ids).cpu().numpy()
                
                outputs = decode_batch(outputs,  skip_special_tokens=True)
                target_ids = decode_batch(target_ids, skip_special_tokens=True )

                # 删除decode出来字符间的空格
                outputs = [sentance.replace(' ', '') for sentance in outputs]
                target_ids = [sentance.replace(' ', '') for sentance in target_ids]

                # print('outputs: {}'.format(outputs[0:5]))
                # print('target_ids: {}'.format(target_ids[0:5]))
                # print()


                bleu4_scores = [get_bleu4_score(reference=target_ids[i], outputs=outputs[i]) for i in range(len(target_ids))]
                bleu4_scores.extend(bleu4_scores)

                # if step >= 10: break
        
        avg_bleu4_score = my_average(bleu4_scores)
        if accelerator.is_main_process:
            progress.update(steps_progress, show_info='bleu4 score: {}'.format(avg_bleu4_score))

        info_txt = 'test_dataset_size: {}, avg_bleu4_score:{}.'.format(len(test_dataset), avg_bleu4_score)
        log.info(info_txt, save_to_file=True)

        return avg_bleu4_score

    
    def print_and_log(self, info: str, accelerator: Accelerator=None) -> None:
        '''
        使用accelerator.print, 否则多进程打印会异常
        '''
        if not accelerator:
            print(info)
        else:
            accelerator.print(info)
        self.logger.info(info, std_out=False, save_to_file=True)

if __name__ == '__main__':
    
    # trainer = ChatTrainer()
    train_config = TrainConfig()
    model_config = T5ModelConfig()

    chat_trainer = ChatTrainer(train_config=train_config, model_config=model_config)

    chat_trainer.train()
    # chat_trainer.test(best_epoch=0)