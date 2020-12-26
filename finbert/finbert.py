﻿# -*- coding: utf-8 -*-
"""Finbert.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1_4HSnIh-dUYBgI0g786HR_YnxHsML1PL
"""

import torch as t
import torch.nn as nn
import torch.nn.functional as F
from transformers import BertForSequenceClassification
from transformers import BertTokenizer 
from transformers import AdamW,get_linear_schedule_with_warmup
from tqdm import tqdm_notebook as tqdm
from tqdm import trange
import random
import numpy as np
import pandas as pd
from finbert.figure import Config,FinSentProcessor
import os


class BaseFinbert(nn.Module):
    def __init__( ### 模型选择不给予用户权限  默认2分类
                self,
                pretrained_model_name_or_path="bert-base-uncased",
                num_labels=2
                ):
        super(BaseFinbert,self).__init__()        
        self.basebert = BertForSequenceClassification.from_pretrained(
                                                                pretrained_model_name_or_path = pretrained_model_name_or_path, 
                                                                num_labels = num_labels
                                                                )
        
    def forward(self,input_ids=None,attention_mask=None,label_ids=None,weights=t.ones(1,2)): ### 提供不平衡处理 默认2分类平衡 
        res = self.basebert(input_ids,attention_mask)#[1]
        logits = F.softmax(res.logits)
        if label_ids != None:
            loss_fct = nn.CrossEntropyLoss(weight = weights)
            loss = loss_fct(logits, label_ids)
            return loss,logits ### logits和loss 是经过softmax后才得到的
        return logits



class Finbert(object):
    def __init__(
                self,                
                label_map={'negative':0,'neutral':1,'positive':2},
                max_seq_length=64,
                train_batch_size=32,
                eval_batch_size=32,
                learning_rate=5e-5,
                num_train_epochs=10.0,
                warm_up_proportion=0.1,
                no_cuda=False,
                do_lower_case=True,  ### 可去
                seed=42,
                local_rank=-1,
                gradient_accumulation_steps=1,
                fp16=False,
                discriminate=True,
                gradual_unfreeze=True,
                encoder_no=12,
                dft_rate=1.2
                ):
        ### 配置参数
        self.config = Config(
                label_map,
                max_seq_length,
                train_batch_size,
                eval_batch_size,
                learning_rate,
                int(num_train_epochs),
                warm_up_proportion,
                no_cuda,
                do_lower_case,
                seed,
                local_rank,
                gradient_accumulation_steps,
                fp16,
                discriminate,
                gradual_unfreeze,
                encoder_no,
                dft_rate
                )
        self.au_config = dict()
        self.finPsor = FinSentProcessor()
        self.model = BaseFinbert(os.getcwd()+'/'+'../models/language_model/',len(self.config.label_map))
        self.tokenizer = BertTokenizer.from_pretrained(os.getcwd()+'/'+'../models/language_model/',do_lower_case=True) 
        self.optimizer = None  ### 需要后续调参后才能创建
        self.schedule = None
        
        ### 得到设备号
        if self.config.local_rank == -1 or self.config.no_cuda:
            self.au_config['device'] = t.device("cuda" if t.cuda.is_available() and not self.config.no_cuda else "cpu")
            self.au_config['n_gpu'] = t.cuda.device_count()
        else:
            t.cuda.set_device(self.config.local_rank)
            self.au_config['device'] = t.device("cuda", self.config.local_rank)
            self.au_config['n_gpu'] = 1
            t.distributed.init_process_group(backend='nccl')
        
        ### 对异常参数进行检测
        if self.config.gradient_accumulation_steps < 1:
            raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            self.config.gradient_accumulation_steps))
            
        self.config.train_batch_size = self.config.train_batch_size // self.config.gradient_accumulation_steps  ### 后处理
        
        ### 设置可复现性
        random.seed(self.config.seed)
        np.random.seed(self.config.seed)
        t.manual_seed(self.config.seed)
        if self.au_config['n_gpu'] > 0:
            t.cuda.manual_seed_all(self.config.seed)
            
        self.model.to(self.au_config['device'])  ### 
        
        # 配置Adamw
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        lr = self.config.learning_rate
        dft_rate = self.config.dft_rate

        if self.config.discriminate:
            encoder_params = []
            for i in range(12):  ### 学习率逐层增加
                encoder_decay = {
                    'params': [p for n, p in list(self.model.basebert.bert.encoder.layer[i].named_parameters()) if
                               not any(nd in n for nd in no_decay)],
                    'weight_decay': 0.01,
                    'lr': lr / (dft_rate ** (12 - i))}
                encoder_nodecay = {
                    'params': [p for n, p in list(self.model.basebert.bert.encoder.layer[i].named_parameters()) if
                               any(nd in n for nd in no_decay)],
                    'weight_decay': 0.0,
                    'lr': lr / (dft_rate ** (12 - i))}
                encoder_params.append(encoder_decay)
                encoder_params.append(encoder_nodecay)

            optimizer_grouped_parameters = [
                {'params': [p for n, p in list(self.model.basebert.bert.embeddings.named_parameters()) if
                            not any(nd in n for nd in no_decay)],
                 'weight_decay': 0.01,
                 'lr': lr / (dft_rate ** 13)},
                {'params': [p for n, p in list(self.model.basebert.bert.embeddings.named_parameters()) if
                            any(nd in n for nd in no_decay)],
                 'weight_decay': 0.0,
                 'lr': lr / (dft_rate ** 13)},
                {'params': [p for n, p in list(self.model.basebert.bert.pooler.named_parameters()) if
                            not any(nd in n for nd in no_decay)],
                 'weight_decay': 0.01,
                 'lr': lr},
                {'params': [p for n, p in list(self.model.basebert.bert.pooler.named_parameters()) if
                            any(nd in n for nd in no_decay)],
                 'weight_decay': 0.0,
                 'lr': lr},
                {'params': [p for n, p in list(self.model.basebert.classifier.named_parameters()) if
                            not any(nd in n for nd in no_decay)],
                 'weight_decay': 0.01,
                 'lr': lr},
                {'params': [p for n, p in list(self.model.basebert.classifier.named_parameters()) if any(nd in n for nd in no_decay)],
                 'weight_decay': 0.0,
                 'lr': lr}]

            optimizer_grouped_parameters.extend(encoder_params)
            
        else:
            param_optimizer = list(self.model.named_parameters())

            optimizer_grouped_parameters = [
                {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
                 'weight_decay': 0.01},
                {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]

        self.optimizer = AdamW(optimizer_grouped_parameters,lr=lr, eps=1e-8)
                

        
    def fit(self,X,y,class_weight=None): 
        
        ###  X和y进行合并成list
        raw_data = list(zip(X.tolist(),y.tolist()))
        
        ### 将原始数据转换成特征数据
        examples = self.finPsor.convert_raws2examples(raw_data)
        features = self.finPsor.convert_examples2features(examples,
                                                     self.tokenizer,
                                                     self.config.max_seq_length,
                                                     self.config.label_map
                                                     )
        
        ## 配置scheduler
        self.au_config['num_train_optimization_steps'] = int(len(examples) / self.config.train_batch_size
                                                             / self.config.gradient_accumulation_steps) * self.config.num_train_epochs            
        self.scheduler = get_linear_schedule_with_warmup(self.optimizer, 
                                                    num_warmup_steps = 0, 
                                                    num_training_steps = self.au_config['num_train_optimization_steps']
                                                    )    
        
        real_step = 0  ### 实际更改模型权重的step次数
        encoder_no = self.config.encoder_no - 1  ### 跟踪解冻痕迹  用户指定第3层向前解冻，实际下标为2  是否应从pooler层开始呢？
        valid_loss_avg_batchs_best = 7000 ### 所有epoch中最佳的泛化/验证误差
        
        
        ### 冷冻bert基准模型的全部层，而Transformers自加的classfier层不冷冻
        if self.config.gradual_unfreeze:
            for param in self.model.basebert.bert.parameters():
                param.requires_grad = False
                
        ### epoch循环开始
        for i in trange(self.config.num_train_epochs,desc='epoch'):
            ### 训练集与测试集分割  dataloader
            dataloaderT,dataloaderV,weightT,weightV = self.finPsor.convert_features2dataloader_T_V(features,
                                                                              self.config.eval_batch_size,
                                                                              is_train = True,
                                                                              train_batch_size = self.config.train_batch_size,
                                                                              rate = 0.8,
                                                                              lable_ids_list = self.config.label_map.values(),
                                                                              class_weight=class_weight
                                                                              )
            ### 开始进行训练
            self.model.train()            
            
            train_loss_avg_batchs = 0   ### 记录训练集每epoch的平均batch损失
            
            ### 在训练过程中逐渐解冻        
            for step,batch in enumerate(tqdm(dataloaderT,desc='Iteration')): ### step不仅控制解冻速度，同时还控制模型权重更新速度
                
                if self.config.gradual_unfreeze and step % (len(dataloaderT)//3) == 0:
                    if encoder_no == -1: ### 若达到Embedding层
                        for param in self.model.basebert.bert.embeddings.parameters():
                            param.requires_grad = True    
                    else: 
                        for param in self.model.basebert.bert.encoder.layer[encoder_no].parameters():
                            param.requires_grad = True                    
                        encoder_no = encoder_no - 1
                            
                input_ids, attention_mask, label_ids = batch
                
                input_ids = input_ids.to(self.au_config['device'])
                attention_mask = attention_mask.to(self.au_config['device'])
                label_ids = label_ids.to(self.au_config['device'])
                
                weightT = weightT.to(self.au_config['device'])
                loss_T,logits_T = self.model(input_ids,attention_mask,label_ids,weightT)
                    
                loss_T = loss_T / self.config.gradient_accumulation_steps
                
                loss_T.backward() ### 积累梯度
                
                train_loss_avg_batchs = train_loss_avg_batchs + loss_T.item()
                
                if (step + 1) % self.config.gradient_accumulation_steps == 0: ### 利用梯度信息更新模型权重
                    self.optimizer.step()
                    self.scheduler.step()
                    self.model.zero_grad()  ### 清空模型的所有当前梯度信息     这步可能有错
                    real_step = real_step + 1
                                                                                              
            train_loss_avg_batchs = train_loss_avg_batchs/len(dataloaderT)  
            print('epoch {} train_loss_avg_batchs: {}'.format(i,train_loss_avg_batchs))
            
            ### 开始进行验证                                                                                  
            self.model.eval()   
            
            val_loss_avg_batchs =0
            
            for input_ids, attention_mask, label_ids in tqdm(dataloaderV,desc='Iteration'):
                
                input_ids = input_ids.to(self.au_config['device'])
                attention_mask = attention_mask.to(self.au_config['device'])
                label_ids = label_ids.to(self.au_config['device'])
                
                with t.no_grad():
                        weightV = weightV.to(self.au_config['device'])
                        loss_V,logits_V = self.model(input_ids,attention_mask,label_ids,weightV)
                    
                val_loss_avg_batchs = val_loss_avg_batchs + loss_V.item()
                
            val_loss_avg_batchs = val_loss_avg_batchs/len(dataloaderV)  # 待输出
            print('epoch {} val_loss_avg_batchs: {}'.format(i,val_loss_avg_batchs))
            
            ### 保留最佳模型状态
            if valid_loss_avg_batchs_best > val_loss_avg_batchs:
                try:
                    os.remove(os.getcwd()+'/'+'../models/classifier_model/temp.bin')
                except:
                    pass
                t.save({'state_dict': self.model.state_dict()},os.getcwd()+'/'+'../models/classifier_model/temp.bin')      
                valid_loss_avg_batchs_best = val_loss_avg_batchs
        
        print('valid_loss_avg_batchs_best: {}'.format(valid_loss_avg_batchs_best))
        ### 存储模型及相关配置    --- epochs循环结束  
        checkpoint = t.load(os.getcwd()+'/'+'../models/classifier_model/temp.bin')
        self.model.load_state_dict(checkpoint['state_dict'])   ### 得到最佳模型    
        
        model_to_save = self.model.module if hasattr(self.model, 'module') else self.model   ### 再次存储模型自身的状态配置
        t.save(model_to_save.state_dict(), os.getcwd()+'/'+'../models/classifier_model/pytorch_model.bin') 
        
        with open(os.getcwd()+'/'+'../models/classifier_model/config.json', 'w') as f:  ### 存储模型自身的config配置
            f.write(model_to_save.basebert.config.to_json_string())  ### Finbert不带config参数，因此使用baebert的config
        os.remove(os.getcwd()+'/'+'../models/classifier_model/temp.bin')
                   
        return self

    def predict_proba(self,X):
        ### 将原始数据转换成特征数据
        raw_data = list(zip(X.tolist(),list([list(self.config.label_map.keys())[0]]*len(X.tolist()))))
        examples = self.finPsor.convert_raws2examples(raw_data)
        features = self.finPsor.convert_examples2features(examples,
                                                     self.tokenizer,
                                                     self.config.max_seq_length,
                                                     self.config.label_map
                                                     )
        
        logits_list = []
        
        dataloaderV = self.finPsor.convert_features2dataloader_T_V(features,
                                                                            self.config.eval_batch_size,
                                                                            is_train = False,
                                                                            )
        self.model.eval()
        for input_ids, attention_mask, _ in tqdm(dataloaderV,desc='Iteration'):
           
          input_ids = input_ids.to(self.au_config['device'])
          attention_mask = attention_mask.to(self.au_config['device'])
                
          with t.no_grad():
            logits_V = self.model(input_ids,attention_mask) 

          logits_V = np.array(logits_V.cpu())
          for line in logits_V:
            logits_list.append(line)

        return pd.DataFrame(logits_list,columns=[list(self.config.label_map.keys())])
    
    def predict(self,X):       
        prec_prob = self.predict_proba(X)
        label_map_reverse = {value:key for key,value in self.config.label_map.items()}
        predictions = [label_map_reverse[x] for x in np.argmax(prec_prob.values,axis=1)]
        return pd.DataFrame(predictions,columns=['prediction'])
    
    def score(self,X,y):
        prec = self.predict(X).iloc[:,0] ### 转成Sries
        prec = prec.values
        y = y.values
        return (prec==y).sum()/y.shape[0]



