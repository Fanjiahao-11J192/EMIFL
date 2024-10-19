"""
最后确定的，我们提出的模型
"""
import torch
import os
import json
from collections import OrderedDict
import torch.nn.functional as F
from models.base_model import BaseModel
from models.networks.fc import FcEncoder
from models.networks.lstm import LSTMEncoder
from models.networks.textcnn import TextCNN
from models.networks.classifier import FcClassifier,Fusion
from models.networks.autoencoder_2 import ResidualAE
# from models.networks.autoencoder import ResidualAE
from models.utt_fusion_model import UttFusionModel
from models.utils.config import OptConfig
from models.networks.shared import SharedEncoder
#from models.MISA_model import MISAModel
from models.pretrained_IFMMIN_model import UttSharedModel


class IFMMINModel(BaseModel):
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        parser.add_argument('--input_dim_a', type=int, default=130, help='acoustic input dim')
        parser.add_argument('--input_dim_l', type=int, default=1024, help='lexical input dim')
        parser.add_argument('--input_dim_v', type=int, default=384, help='lexical input dim')
        parser.add_argument('--embd_size_a', default=128, type=int, help='audio model embedding size')
        parser.add_argument('--embd_size_l', default=128, type=int, help='text model embedding size')
        parser.add_argument('--embd_size_v', default=128, type=int, help='visual model embedding size')
        parser.add_argument('--embd_method_a', default='maxpool', type=str, choices=['last', 'maxpool', 'attention'], \
                            help='audio embedding method,last,mean or atten')
        parser.add_argument('--embd_method_v', default='maxpool', type=str, choices=['last', 'maxpool', 'attention'], \
                            help='visual embedding method,last,mean or atten')
        parser.add_argument('--AE_layers', type=str, default='128,64,32',
                            help='256,128 for 2 layers with 256, 128 nodes respectively')
        parser.add_argument('--n_blocks', type=int, default=3, help='number of AE blocks')
        parser.add_argument('--cls_layers', type=str, default='128,128',
                            help='256,128 for 2 layers with 256, 128 nodes respectively')
        parser.add_argument('--dropout_rate', type=float, default=0.3, help='rate of dropout')
        parser.add_argument('--bn', action='store_true', help='if specified, use bn layers in FC')
        parser.add_argument('--pretrained_path', type=str, help='where to load pretrained encoder network')
        parser.add_argument('--pretrained_invariant_path', type=str,
                            help='where to load pretrained invariant encoder network')
        parser.add_argument('--ce_weight', type=float, default=1.0, help='weight of ce loss')
        parser.add_argument('--mse_weight', type=float, default=1.0, help='weight of mse loss')
        parser.add_argument('--cycle_weight', type=float, default=1.0, help='weight of cycle loss')
        parser.add_argument('--invariant_weight', type=float, default=1.0, help='weight of invariant loss')
        parser.add_argument('--share_weight', action='store_true',
                            help='share weight of forward and backward autoencoders')
        parser.add_argument('--image_dir', type=str, default='./invariant_image', help='models image are saved here')
        parser.add_argument('--transformer_heads', default=4, type=int, help='transformer multimodal head numbers')
        parser.add_argument('--transformer_layers', default=6, type=int, help='transformer encode layers numbers')
        return parser

    def __init__(self, opt):
        """Initialize the LSTM autoencoder class
        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        super().__init__(opt)
        # our expriment is on 10 fold setting, teacher is on 5 fold setting, the train set should match
        self.loss_names = ['CE', 'mse', 'invariant']
        self.model_names = ['C', 'AE', 'invariant']  # 六个模块的名称

        # acoustic model
        self.netA = LSTMEncoder(opt.input_dim_a, opt.embd_size_a, embd_method=opt.embd_method_a)
        self.model_names.append('A')
        # lexical model 文本
        self.netL = TextCNN(opt.input_dim_l, opt.embd_size_l)
        self.model_names.append('L')
        # visual model
        self.netV = LSTMEncoder(opt.input_dim_v, opt.embd_size_v, opt.embd_method_v)
        self.model_names.append('V')
        # # AE model  级联残差自编码器

        AE_layers = list(map(lambda x: int(x), opt.AE_layers.split(',')))
        AE_input_dim = opt.embd_size_a + opt.embd_size_v + opt.embd_size_l
        self.netAE = ResidualAE(AE_layers, opt.n_blocks, AE_input_dim, dropout=0, use_bn=False)
        # 分类层
        cls_layers = list(map(lambda x: int(x), opt.cls_layers.split(',')))
        cls_input_size = AE_layers[-1] * opt.n_blocks
        if self.opt.corpus_name != 'MOSI':
            self.netC = FcClassifier(cls_input_size, cls_layers, output_dim=opt.output_dim, dropout=opt.dropout_rate,
                                 use_bn=opt.bn)
        else:
            self.netC = Fusion(cls_input_size, cls_layers, output_dim=opt.output_dim, dropout=opt.dropout_rate)
        # 共性特征提取网络
        self.netinvariant = SharedEncoder(opt)

        if self.isTrain:
            self.load_pretrained_encoder(opt)
            if self.opt.corpus_name != 'MOSI':
                self.criterion_ce = torch.nn.CrossEntropyLoss()
            else:
                self.criterion_ce = torch.nn.MSELoss()
            self.criterion_mse = torch.nn.MSELoss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            paremeters = [{'params': getattr(self, 'net' + net).parameters()} for net in self.model_names]
            self.optimizer = torch.optim.Adam(paremeters, lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer)
            self.output_dim = opt.output_dim
            self.ce_weight = opt.ce_weight
            self.mse_weight = opt.mse_weight
            self.invariant_weight = opt.invariant_weight

        else:
            self.load_pretrained_encoder(opt)

        # modify save_dir
        self.save_dir = os.path.join(self.save_dir, str(opt.cvNo))
        print(self.save_dir)
        if not os.path.exists(self.save_dir):
            os.mkdir(self.save_dir)

        image_save_dir = os.path.join(opt.image_dir, opt.name)
        image_save_dir = os.path.join(image_save_dir, str(opt.cvNo))
        self.predict_image_save_dir = os.path.join(image_save_dir, 'predict')
        self.invariant_image_save_dir = os.path.join(image_save_dir, 'invariant')
        self.loss_image_save_dir = os.path.join(image_save_dir, 'loss')
        if not os.path.exists(self.predict_image_save_dir):
            os.makedirs(self.predict_image_save_dir)
        if not os.path.exists(self.invariant_image_save_dir):
            os.makedirs(self.invariant_image_save_dir)
        if not os.path.exists(self.loss_image_save_dir):
            os.makedirs(self.loss_image_save_dir)

    # 加载预训练Encoder，
    def load_pretrained_encoder(self, opt):
        print('Init parameter from {}'.format(opt.pretrained_path))
        pretrained_path = os.path.join(opt.pretrained_path, str(opt.cvNo))
        pretrained_config_path = os.path.join(opt.pretrained_path, 'train_opt.conf')
        pretrained_config = self.load_from_opt_record(pretrained_config_path)
        pretrained_config.isTrain = False  # teacher model should be in test mode
        pretrained_config.gpu_ids = opt.gpu_ids  # set gpu to the same
        self.pretrained_encoder = UttSharedModel(pretrained_config)
        self.pretrained_encoder.load_networks_cv(pretrained_path)
        self.pretrained_encoder.cuda()
        self.pretrained_encoder.eval()


    def post_process(self):
        # called after model.setup()
        def transform_key_for_parallel(state_dict):
            return OrderedDict([('module.' + key, value) for key, value in state_dict.items()])

        if self.isTrain:
            print('[ Init ] Load parameters from pretrained encoder network')
            f = lambda x: transform_key_for_parallel(x)
            self.netA.load_state_dict(f(self.pretrained_encoder.netA.state_dict()))
            self.netV.load_state_dict(f(self.pretrained_encoder.netV.state_dict()))
            self.netL.load_state_dict(f(self.pretrained_encoder.netL.state_dict()))
            self.netinvariant.load_state_dict(f(self.pretrained_encoder.netShared.state_dict()))

    def load_from_opt_record(self, file_path):
        opt_content = json.load(open(file_path, 'r'))
        opt = OptConfig()
        opt.load(opt_content)
        return opt

    def set_input(self, input):
        """
        Unpack input data from the dataloader and perform necessary pre-processing steps.
        Parameters:
            input (dict): include the data itself and its metadata information.
        """
        self.acoustic = acoustic = input['A_feat'].float().to(self.device)
        self.lexical = lexical = input['L_feat'].float().to(self.device)
        self.visual = visual = input['V_feat'].float().to(self.device)

        if self.isTrain:
            self.label = input['label'].to(self.device)
            self.missing_index = input['missing_index'].long().to(self.device)  # [a,v,l]
            # A modality
            self.A_miss_index = self.missing_index[:, 0].unsqueeze(1).unsqueeze(2)
            self.A_miss = acoustic * self.A_miss_index
            self.A_reverse = acoustic * -1 * (self.A_miss_index - 1)
            # V modality
            self.V_miss_index = self.missing_index[:, 1].unsqueeze(1).unsqueeze(2)
            self.V_miss = visual * self.V_miss_index
            self.V_reverse = visual * -1 * (self.V_miss_index - 1)
            # L modality
            self.L_miss_index = self.missing_index[:, 2].unsqueeze(1).unsqueeze(2)
            self.L_miss = lexical * self.L_miss_index
            self.L_reverse = lexical * -1 * (self.L_miss_index - 1)
            if self.opt.corpus_name == 'MOSI':
                self.label = self.label.unsqueeze(1)
        else:
            self.A_miss = acoustic
            self.V_miss = visual
            self.L_miss = lexical


    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        # get utt level representattion
        self.feat_A_miss,_ = self.netA(self.A_miss)  # 缺失视频特征
        self.feat_L_miss,_ = self.netL(self.L_miss)
        self.feat_V_miss,_ = self.netV(self.V_miss)
        # fusion miss
        self.feat_fusion_miss = torch.cat([self.feat_A_miss, self.feat_L_miss, self.feat_V_miss], dim=-1)

        self.feat_A_invariant = self.netinvariant(self.feat_A_miss)
        self.feat_L_invariant = self.netinvariant(self.feat_L_miss)
        self.feat_V_invariant = self.netinvariant(self.feat_V_miss)
        self.invariant_miss = torch.cat([self.feat_A_invariant, self.feat_L_invariant, self.feat_V_invariant], dim=-1)

        self.recon_fusion, self.latent = self.netAE(self.feat_fusion_miss, self.invariant_miss)

        # get fusion outputs for missing modality
        self.logits, _ = self.netC(self.latent)
        if self.opt.corpus_name != 'MOSI':
            self.pred = F.softmax(self.logits, dim=-1)
        else:
            self.pred = self.logits
        # for training 
        if self.isTrain:
            with torch.no_grad():
                self.T_embd_A,_ = self.pretrained_encoder.netA(self.A_reverse)
                self.T_embd_L,_ = self.pretrained_encoder.netL(self.L_reverse)
                self.T_embd_V,_ = self.pretrained_encoder.netV(self.V_reverse)
                self.T_embds = torch.cat([self.T_embd_A, self.T_embd_L, self.T_embd_V], dim=-1)

                # invariant_missing_num = self.missing_index.sum(dim=1).unsqueeze(1)
                # self.invariant_missing_utt = (self.feat_L_invariant + self.feat_V_invariant + self.feat_A_invariant) / invariant_missing_num

                embd_A,_ = self.pretrained_encoder.netA(self.acoustic)
                embd_L,_ = self.pretrained_encoder.netL(self.lexical)
                embd_V,_ = self.pretrained_encoder.netV(self.visual)

                embd_A_invariant = self.pretrained_encoder.netShared(embd_A)
                embd_L_invariant = self.pretrained_encoder.netShared(embd_L)
                embd_V_invariant = self.pretrained_encoder.netShared(embd_V)
                self.invariant = torch.cat([embd_A_invariant, embd_L_invariant, embd_V_invariant], dim=-1)
                # self.invariant = (embd_V_invariant + embd_A_invariant + embd_L_invariant) / 3.0

    def backward(self):
        """Calculate the loss for back propagation"""
        # 分类损失
        self.loss_CE = self.ce_weight * self.criterion_ce(self.logits, self.label)
        # forward损失
        self.loss_mse = self.mse_weight * self.criterion_mse(self.T_embds, self.recon_fusion)
        # 占位，共性特征损失
        self.loss_invariant = self.invariant_weight * self.criterion_mse(self.invariant, self.invariant_miss)
        # self.loss_invariant = self.invariant_weight * self.criterion_mse(self.invariant, self.invariant_missing_utt)
        # 综合损失
        loss = self.loss_CE + self.loss_mse + self.loss_invariant
        loss.backward()
        for model in self.model_names:
            torch.nn.utils.clip_grad_norm_(getattr(self, 'net' + model).parameters(), 1.0)

    def optimize_parameters(self, epoch):
        """Calculate losses, gradients, and update network weights; called in every training iteration"""
        # forward
        self.forward()
        # backward
        self.optimizer.zero_grad()
        self.backward()
        self.optimizer.step()
