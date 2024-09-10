import os
import time
import numpy as np
from opts.get_opts import Options
from data import create_dataset_with_args
from models import create_model
from utils.logger import get_logger, ResultRecorder,ResultRecorderMOSI
from sklearn.metrics import accuracy_score, recall_score, f1_score, confusion_matrix
import torch
import random
from torch.utils.tensorboard import SummaryWriter
def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)

def make_path(path):
    if not os.path.exists(path):
        os.makedirs(path)

def eval(model, val_iter, is_save=False, phase='test', mode=None):
    model.eval()
    total_pred = []
    total_label = []
    
    for i, data in enumerate(val_iter):  # inner loop within one epoch
        model.set_input(data)         # unpack data from dataset and apply preprocessing
        model.test()
        if model.opt.corpus_name != 'MOSI':
            pred = model.pred.argmax(dim=1).detach().cpu().numpy()
        else:
            pred = model.pred.detach().cpu().numpy()
        label = data['label']
        total_pred.append(pred)
        total_label.append(label)
    
    # calculate metrics
    total_pred = np.concatenate(total_pred)
    total_label = np.concatenate(total_label)
    if model.opt.corpus_name != 'MOSI':
        acc = accuracy_score(total_label, total_pred)
        uar = recall_score(total_label, total_pred, average='macro')
        f1 = f1_score(total_label, total_pred, average='macro')
        cm = confusion_matrix(total_label, total_pred)
        model.train()

        # save test results
        if is_save:
            save_dir = model.save_dir
            np.save(os.path.join(save_dir, '{}_pred.npy'.format(phase)), total_pred)
            np.save(os.path.join(save_dir, '{}_label.npy'.format(phase)), total_label)
        return acc, uar, f1, cm
    else:
        # f1 = f1_score(total_label, total_pred, average='macro')
        accuracy, mae, corr, f_score = calc_metrics(total_label, total_pred, mode)
        model.train()

        # save test results
        if is_save:
            save_dir = model.save_dir
            np.save(os.path.join(save_dir, '{}_pred.npy'.format(phase)), total_pred)
            np.save(os.path.join(save_dir, '{}_label.npy'.format(phase)), total_label)
        return accuracy, mae, corr,f_score

    
def calc_metrics(y_true, y_pred, mode=None, to_print=False):
    """
    Metric scheme adapted from:
    https://github.com/yaohungt/Multimodal-Transformer/blob/master/src/eval_metrics.py
    """

    test_preds = y_pred.squeeze(1)
    test_truth = y_true

    non_zeros = np.array([i for i, e in enumerate(test_truth) if e != 0])

    test_preds_a7 = np.clip(test_preds, a_min=-3., a_max=3.)
    test_truth_a7 = np.clip(test_truth, a_min=-3., a_max=3.)
    test_preds_a5 = np.clip(test_preds, a_min=-2., a_max=2.)
    test_truth_a5 = np.clip(test_truth, a_min=-2., a_max=2.)

    mae = np.mean(np.absolute(test_preds - test_truth))  # Average L1 distance between preds and truths
    corr = np.corrcoef(test_preds, test_truth)[0][1]

    f_score = f1_score((test_preds[non_zeros] > 0), (test_truth[non_zeros] > 0), average='weighted')

    # pos - neg
    # binary_truth = (test_truth[non_zeros] > 0)
    # binary_preds = (test_preds[non_zeros] > 0)

    # non-neg - neg
    binary_truth = (test_truth >= 0)
    binary_preds = (test_preds >= 0)

    return accuracy_score(binary_truth, binary_preds), mae,corr, f_score

def clean_chekpoints(expr_name, store_epoch):
    root = os.path.join(opt.checkpoints_dir, expr_name)
    for checkpoint in os.listdir(root):
        if not checkpoint.startswith(str(store_epoch)+'_') and checkpoint.endswith('pth'):
            os.remove(os.path.join(root, checkpoint))

if __name__ == '__main__':
    setup_seed(2023)

    opt = Options().parse()                             # get training options
    logger_path = os.path.join(opt.log_dir, opt.name, str(opt.cvNo)) # get logger path
    if not os.path.exists(logger_path):                 # make sure logger path exists
        os.mkdir(logger_path)
    # 设置可视化结果路径
    writer = SummaryWriter(logger_path)

    total_cv = 10 if opt.corpus_name != 'MSP' else 12
    if opt.corpus_name != 'MOSI':
        result_recorder = ResultRecorder(os.path.join(opt.log_dir, opt.name, 'result.tsv'), total_cv=total_cv) # init result recoreder
    else:
        result_recorder = ResultRecorderMOSI(os.path.join(opt.log_dir, opt.name, 'result.tsv'), total_cv=total_cv)
    suffix = '_'.join([opt.model, opt.dataset_mode])    # get logger suffix
    logger = get_logger(logger_path, suffix)            # get logger
    if opt.has_test:                                    # create a dataset given opt.dataset_mode and other options
        dataset, val_dataset, tst_dataset = create_dataset_with_args(opt, set_name=['trn', 'val', 'tst'])  
    else:
        dataset, val_dataset = create_dataset_with_args(opt, set_name=['trn', 'val'])
    
    dataset_size = len(dataset)    # get the number of images in the dataset.
    logger.info('The number of training samples = %d' % dataset_size)
    model = create_model(opt)      # create a model given opt.model and other options
    model.setup(opt)               # regular setup: load and print networks; create schedulers
    total_iters = 0                # the total number of training iterations
    best_eval_acc, best_eval_uar, best_eval_f1, best_eval_corr, best_eval_mae = 0, 0, 0, 0, 10
    best_eval_epoch = -1           # record the best eval epoch

    for epoch in range(opt.epoch_count, opt.niter + opt.niter_decay + 1):    # outer loop for different epochs; we save the model by <epoch_count>, <epoch_count>+<save_latest_freq>
        epoch_start_time = time.time()  # timer for entire epoch
        iter_data_time = time.time()    # timer for data loading per iteration
        epoch_iter = 0                  # the number of training iterations in current epoch, reset to 0 every epoch

        for i, data in enumerate(dataset):  # inner loop within one epoch
            iter_start_time = time.time()   # timer for computation per iteration
            total_iters += 1                # opt.batch_size
            epoch_iter += opt.batch_size
            model.set_input(data)           # unpack data from dataset and apply preprocessing
            model.optimize_parameters(epoch)   # calculate loss functions, get gradients, update network weights
                
            if total_iters % opt.print_freq == 0:    # print training losses and save logging information to the disk
                losses = model.get_current_losses()
                t_comp = (time.time() - iter_start_time) / opt.batch_size
                logger.info('Cur epoch {}'.format(epoch) + ' loss ' + 
                        ' '.join(map(lambda x:'{}:{{{}:.4f}}'.format(x, x), model.loss_names)).format(**losses))
                writer.add_scalar("train_loss_CE", losses['CE'], global_step=total_iters)
                if 'CMD' in losses:
                    writer.add_scalar("train_loss_CMD", losses['CMD'], global_step=total_iters)
                if 'KL' in losses:
                    writer.add_scalar("train_loss_KL", losses['KL'], global_step=total_iters)
                if 'TA' in losses:
                    writer.add_scalar("train_loss_TA", losses['TA'], global_step=total_iters)
                if 'TV' in losses:
                    writer.add_scalar("train_loss_TV", losses['TV'], global_step=total_iters)
                if 'VA' in losses:
                    writer.add_scalar("train_loss_VA", losses['VA'], global_step=total_iters)
                #writer.add_scalar("train_loss_DIFF", losses['DIFF'], global_step=total_iters)

            iter_data_time = time.time()
        if epoch % opt.save_epoch_freq == 0:              # cache our model every <save_epoch_freq> epochs
            logger.info('saving the model at the end of epoch %d, iters %d' % (epoch, total_iters))
            model.save_networks('latest')
            model.save_networks(epoch)

        logger.info('End of training epoch %d / %d \t Time Taken: %d sec' % (epoch, opt.niter + opt.niter_decay, time.time() - epoch_start_time))
        model.update_learning_rate(logger,writer,epoch)                     # update learning rates at the end of every epoch.

        # eval val set
        if opt.corpus_name != 'MOSI':
            acc, uar, f1, cm = eval(model, val_dataset)
            logger.info('Val result of epoch %d / %d acc %.4f uar %.4f f1 %.4f' % (epoch, opt.niter + opt.niter_decay, acc, uar, f1))
            logger.info('\n{}'.format(cm))

            # 记录训练过程
            writer.add_scalar("val_acc", acc, global_step=epoch)
            writer.add_scalar("val_uar", uar, global_step=epoch)
            writer.add_scalar("val_f1", f1, global_step=epoch)
        else:
            acc, mae,corr,f1 = eval(model, val_dataset)
            logger.info('Val result of epoch %d / %d acc %.4f MAE %.4f corr %.4f f1 %.4f' % (epoch, opt.niter + opt.niter_decay, acc, mae,corr, f1))
            # 记录训练过程
            writer.add_scalar("val_acc", acc, global_step=epoch)
            writer.add_scalar("val_mae", mae, global_step=epoch)
            writer.add_scalar("val_corr", corr, global_step=epoch)
            writer.add_scalar("val_f1", f1, global_step=epoch)

        # show test result for debugging
        if opt.has_test and opt.verbose:
            if opt.corpus_name != 'MOSI':
                acc, uar, f1, cm = eval(model, tst_dataset)
                logger.info('Tst result of epoch %d / %d acc %.4f uar %.4f f1 %.4f' % (epoch, opt.niter + opt.niter_decay, acc, uar, f1))
                logger.info('\n{}'.format(cm))

                writer.add_scalar("test_acc", acc, global_step=epoch)
                writer.add_scalar("test_uar", uar, global_step=epoch)
                writer.add_scalar("test_f1", f1, global_step=epoch)
            else:
                acc, mae,corr,f1 = eval(model, tst_dataset)
                logger.info('Tst result of epoch %d / %d acc %.4f MAE %.4f corr %.4f f1 %.4f' % (epoch, opt.niter + opt.niter_decay, acc, mae, corr,f1))
                writer.add_scalar("test_acc", acc, global_step=epoch)
                writer.add_scalar("test_mae", mae, global_step=epoch)
                writer.add_scalar("test_corr", corr, global_step=epoch)
                writer.add_scalar("test_f1", f1, global_step=epoch)

        # record epoch with best result
        if opt.corpus_name == 'IEMOCAP':
            if uar > best_eval_uar:  
                best_eval_epoch = epoch
                best_eval_uar = uar
                best_eval_acc = acc
                best_eval_f1 = f1
            select_metric = 'uar'
            best_metric = best_eval_uar
        elif opt.corpus_name == 'MSP':
            if f1 > best_eval_f1:
                best_eval_epoch = epoch
                best_eval_uar = uar
                best_eval_acc = acc
                best_eval_f1 = f1
            select_metric = 'f1'
            best_metric = best_eval_f1
        elif opt.corpus_name == 'MOSI':
            if mae < best_eval_mae:
                best_eval_epoch = epoch
                best_eval_acc = acc
                best_eval_mae = mae
                best_eval_corr = corr
                best_eval_f1 = f1
            select_metric = 'MAE'
            best_metric = best_eval_mae
        else:
            raise ValueError(f'corpus name must be IEMOCAP,MSP or MOSI, but got {opt.corpus_name}')

    writer.close()
    # print best eval result
    logger.info('Best eval epoch %d found with %s %f' % (best_eval_epoch, select_metric, best_metric))

    # test
    if opt.has_test:
        logger.info('Loading best model found on val set: epoch-%d' % best_eval_epoch)
        model.load_networks(best_eval_epoch)
        if opt.corpus_name != 'MOSI':
            _ = eval(model, val_dataset, is_save=True, phase='val')
            acc, uar, f1, cm = eval(model, tst_dataset, is_save=True, phase='test')
            logger.info('Tst result acc %.4f uar %.4f f1 %.4f' % (acc, uar, f1))
            logger.info('\n{}'.format(cm))
            result_recorder.write_result_to_tsv({
                'acc': acc,
                'uar': uar,
                'f1': f1
            }, cvNo=opt.cvNo)
        else:
            _ = eval(model, val_dataset, is_save=True, phase='val')
            acc, mae, corr, f1 = eval(model, tst_dataset, is_save=True, phase='test')
            logger.info('Tst result acc %.4f MAE %.4f corr %.4f f1 %.4f' % (acc, mae,corr,f1))
            # logger.info('\n{}'.format(cm))
            result_recorder.write_result_to_tsv({
                'acc': acc,
                'MAE': mae,
                'corr':corr,
                'f1': f1
            }, cvNo=opt.cvNo)
    else:
        if opt.corpus_name != 'MOSI':
            result_recorder.write_result_to_tsv({
                'acc': best_eval_acc,
                'uar': best_eval_uar,
                'f1': best_eval_f1
            }, cvNo=opt.cvNo)
        else:
            result_recorder.write_result_to_tsv({
                'acc': best_eval_acc,
                'MAE': best_eval_mae,
                'corr':best_eval_corr,
                'f1': best_eval_f1
            }, cvNo=opt.cvNo)
    
    clean_chekpoints(opt.name + '/' + str(opt.cvNo), best_eval_epoch)
