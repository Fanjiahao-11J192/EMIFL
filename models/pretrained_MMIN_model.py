import argparse
import torch
import os
import json
import torch.nn.functional as F
from models.base_model import BaseModel
from models.networks.fc import FcEncoder
from models.networks.lstm import LSTMEncoder
from models.networks.textcnn import TextCNN
from models.networks.classifier import FcClassifier
from models.networks.Transformer.transformer import TransformerEncoder
from models.utils.functions import CMD,DiffLoss
from torch import nn
from models.networks.SelfAttention import SelfAttn
def str2bool(v):
    """string to boolean"""
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')
class UttFusionModel(BaseModel):
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        parser.add_argument('--input_dim_a', type=int, default=130, help='acoustic input dim')
        parser.add_argument('--input_dim_l', type=int, default=1024, help='lexical input dim')
        parser.add_argument('--input_dim_v', type=int, default=384, help='visual input dim')
        parser.add_argument('--embd_size_a', default=128, type=int, help='audio model embedding size')
        parser.add_argument('--embd_size_l', default=128, type=int, help='text model embedding size')
        parser.add_argument('--embd_size_v', default=128, type=int, help='visual model embedding size')
        parser.add_argument('--embd_method_a', default='maxpool', type=str, choices=['last', 'maxpool', 'attention','avgpool'], \
            help='audio embedding method,last,mean or atten')
        parser.add_argument('--embd_method_v', default='maxpool', type=str, choices=['last', 'maxpool', 'attention','avgpool'], \
            help='visual embedding method,last,mean or atten')
        parser.add_argument('--cls_layers', type=str, default='128,128', help='256,128 for 2 layers with 256, 128 nodes respectively')
        parser.add_argument('--dropout_rate', type=float, default=0.3, help='rate of dropout')
        parser.add_argument('--bn', action='store_true', help='if specified, use bn layers in FC')
        parser.add_argument('--modality', type=str, help='which modality to use for model')
        parser.add_argument('--conv_dim_a', default=40, type=int, help='acoustic conv dim')
        parser.add_argument('--conv_dim_v', default=40, type=int, help='acoustic conv dim')
        parser.add_argument('--conv_dim_l', default=40, type=int, help='acoustic conv dim')
        parser.add_argument('--use_cmd_sim', type=str2bool, default=True)  # 是否使用cmd_sim，默认为使用
        parser.add_argument('--image_dir', type=str, default='./utt_fusion_image', help='models image are saved here')
        return parser

    def __init__(self, opt):
        """Initialize the LSTM autoencoder class
        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        super().__init__(opt)
        # our expriment is on 10 fold setting, teacher is on 5 fold setting, the train set should match
        #self.loss_names = ['CE']
        self.loss_names = ['CE','CMD']
        self.modality = opt.modality
        self.model_names = ['C']
        cls_layers = list(map(lambda x: int(x), opt.cls_layers.split(',')))
        cls_input_size = opt.embd_size_a * int("A" in self.modality) + \
                         opt.embd_size_v * int("V" in self.modality) + \
                         opt.embd_size_l * int("L" in self.modality)
        self.netC = FcClassifier(cls_input_size, cls_layers, output_dim=opt.output_dim,dropout=opt.dropout_rate, use_bn=opt.bn)

        # acoustic model
        if 'A' in self.modality:
            self.model_names.append('A')
            self.netA = LSTMEncoder(opt.input_dim_a, opt.embd_size_a, embd_method=opt.embd_method_a)
            
        # lexical model
        if 'L' in self.modality:
            self.model_names.append('L')
            self.netL = TextCNN(opt.input_dim_l, opt.embd_size_l)
            
        # visual model
        if 'V' in self.modality:
            self.model_names.append('V')
            self.netV = LSTMEncoder(opt.input_dim_v, opt.embd_size_v, opt.embd_method_v)

        self.loss_diff_func = DiffLoss()
        self.loss_cmd_func = CMD()
        if self.isTrain:
            if self.opt.corpus_name != 'MOSI':
                self.criterion_ce = torch.nn.CrossEntropyLoss()
            else:
                self.criterion_ce = torch.nn.MSELoss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            paremeters = [{'params': getattr(self, 'net'+net).parameters()} for net in self.model_names]
            self.optimizer = torch.optim.Adam(paremeters, lr=opt.lr, betas=(opt.beta1, 0.999), weight_decay=opt.weight_decay)
            self.optimizers.append(self.optimizer)
            self.output_dim = opt.output_dim

        # modify save_dir
        self.save_dir = os.path.join(self.save_dir, str(opt.cvNo))
        if not os.path.exists(self.save_dir):
            os.mkdir(self.save_dir)
    
    def set_input(self, input):
        """
        Unpack input data from the dataloader and perform necessary pre-processing steps.
        Parameters:
            input (dict): include the data itself and its metadata information.
        """
        if 'A' in self.modality:
            self.acoustic = input['A_feat'].float().to(self.device)
        if 'L' in self.modality:
            self.lexical = input['L_feat'].float().to(self.device)
        if 'V' in self.modality:
            self.visual = input['V_feat'].float().to(self.device)
        
        self.label = input['label'].to(self.device)
        if self.opt.corpus_name == 'MOSI':
            self.label = self.label.unsqueeze(1)

    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        final_embd = []
        if 'A' in self.modality:
            self.feat_A ,self.feat_A_NoMaxPool = self.netA(self.acoustic)
            final_embd.append(self.feat_A)

        if 'L' in self.modality:
            self.feat_L,self.feat_L_NoMaxPool = self.netL(self.lexical)
            final_embd.append(self.feat_L)
    
        if 'V' in self.modality:
            self.feat_V,self.feat_V_NoMaxPool = self.netV(self.visual)
            final_embd.append(self.feat_V)
      
        # get model outputs
        self.feat = torch.cat(final_embd, dim=-1)
        self.logits, self.ef_fusion_feat = self.netC(self.feat)
        if self.opt.corpus_name != "MOSI":
            self.pred = F.softmax(self.logits, dim=-1)
        else:
            self.pred = self.logits
        
    def backward(self):
        """Calculate the loss for back propagation"""
        self.loss_CE = self.criterion_ce(self.logits, self.label)
        loss = self.loss_CE
        loss.backward()
        for model in self.model_names:
            torch.nn.utils.clip_grad_norm_(getattr(self, 'net'+model).parameters(), 0.5)

    def optimize_parameters(self, epoch):
        """Calculate losses, gradients, and update network weights; called in every training iteration"""
        # forward
        self.forward()   
        # backward
        self.optimizer.zero_grad()  
        self.backward()            
        self.optimizer.step() 

    def get_cmd_loss(self, ):

        if not self.opt.use_cmd_sim:
            return 0.0

        # losses between shared states
        loss = self.loss_cmd_func(self.final_V, self.final_L, 5)
        loss += self.loss_cmd_func(self.final_L, self.final_A, 5)
        loss += self.loss_cmd_func(self.final_A, self.final_V, 5)
        loss = loss / 3.0

        #loss = self.loss_cmd_func(self.feat, self.feat_transformer_fusion, 5)

        return loss

    def get_diff_loss(self):

        shared_t = self.final_L
        shared_v = self.final_V
        shared_a = self.final_A

        private_t = self.feat_L
        private_v = self.feat_V
        private_a = self.feat_A

        # Between private and shared
        loss = self.loss_diff_func(private_t, shared_t)
        loss += self.loss_diff_func(private_v, shared_v)
        loss += self.loss_diff_func(private_a, shared_a)

        # Across privates
        loss += self.loss_diff_func(private_a, private_t)
        loss += self.loss_diff_func(private_a, private_v)
        loss += self.loss_diff_func(private_t, private_v)

        return loss