import itertools
import torch
import os
import numpy as np
import tqdm
from collections import defaultdict
from time import gmtime, strftime
import sys

import torch.nn.functional as F


def get_logging_print(fname):
    cur_time = strftime("%m-%d_%H:%M:%S", gmtime())

    def print_func(*args):
        str_to_write = ' '.join(map(str, args))
        filename = fname % cur_time if '%s' in fname else fname
        with open(filename, 'a') as f:
            f.write(str_to_write + '\n')
            f.flush()

        print(str_to_write)
        sys.stdout.flush()

    return print_func, fname % cur_time if '%s' in fname else fname


def flatten(lst):
    tmp = [i.contiguous().view(-1, 1) for i in lst]
    return torch.cat(tmp).view(-1)


def unflatten_like(vector, likeTensorList):
    # Takes a flat torch.tensor and unflattens it to a list of torch.tensors
    #    shaped like likeTensorList
    outList = []
    i = 0
    for tensor in likeTensorList:
        #n = module._parameters[name].numel()
        n = tensor.numel()
        outList.append(vector[:, i:i+n].view(tensor.shape))
        i += n
    return outList


def LogSumExp(x, dim=0):
    m, _ = torch.max(x, dim=dim, keepdim=True)
    return m + torch.log((x - m).exp().sum(dim=dim, keepdim=True))


def adjust_learning_rate(optimizer, lr):
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def save_checkpoint(dir, epoch, name='checkpoint', **kwargs):
    state = {
        'epoch': epoch,
    }
    state.update(kwargs)
    if not os.path.exists(dir):
        os.makedirs(dir)

    filepath = os.path.join(dir, '%s.pt' % name)
    torch.save(state, filepath)


def train_epoch(loader, model, criterion, optimizer,
                regression=False, verbose=False, subset=None, regularizer=None, scheduler=None, 
                criteria='combined', single_net=False):


    loss_sum = 0.0
    stats_sum = defaultdict(float)
    correct = 0.0
    verb_stage = 0

    num_objects_current = 0
    num_batches = len(loader)
    label_map = {-1: 1, 1: 0}
    model.train()

    if subset is not None:
        num_batches = int(num_batches * subset)
        loader = itertools.islice(loader, num_batches)


    loader = tqdm.tqdm(loader, total=num_batches)

    for i, batch in enumerate(loader):
        input_ids1 = batch["input_ids1"]
        attention_mask1 = batch["attention_mask1"]
        token_type_ids1 = batch["token_type_ids1"]

        input_ids2 = batch["input_ids2"]
        attention_mask2 = batch["attention_mask2"]
        token_type_ids2 = batch["token_type_ids2"]
        target = batch['targets']
        score1 = batch['score1']
        score2 = batch['score2']
        acc1 = batch['acc1']
        acc2 = batch['acc2']
        target_comb = (target, score1, score2, acc1, acc2)
        if torch.cuda.is_available():
            input_ids1 = input_ids1.cuda(non_blocking=True)
            attention_mask1 = attention_mask1.cuda(non_blocking=True)
            token_type_ids1 = token_type_ids1.cuda(non_blocking=True)

            input_ids2 = input_ids2.cuda(non_blocking=True)
            attention_mask2 = attention_mask2.cuda(non_blocking=True)
            token_type_ids2 = token_type_ids2.cuda(non_blocking=True)
            target = target.cuda(non_blocking=True)
            score1 = score1.cuda(non_blocking=True)
            score2 = score2.cuda(non_blocking=True)
            acc1 = acc1.cuda(non_blocking=True)
            acc2 = acc2.cuda(non_blocking=True)            

        loss, output, stats = criterion(model, target_comb, input_ids1=input_ids1, input_ids2=input_ids2,
                                        attention_mask1=attention_mask1, attention_mask2=attention_mask2,
                                        token_type_ids1=token_type_ids1, token_type_ids2=token_type_ids2)                                 
        if regularizer:
            loss += regularizer(model)

        optimizer.zero_grad()
        # for name, param in model.named_parameters():
        #     if 'W1' in name:
        #         print(name, param)
        # print(loss.item())
        loss.backward()
        # for name, param in model.named_parameters():
        #     if 'W1' in name:
        #         print(name, param)
        optimizer.step()
        if scheduler:
            scheduler.step()
        loss_sum += loss.item() * input_ids1.size(0)
        for key, value in stats.items():
            stats_sum[key] += value * input_ids1.size(0)

        if not regression:
            # print('output is', output)
            # print('size of output is', output.size())
            cpu_output = output.detach().cpu().numpy()
            pred = np.argmax(cpu_output, axis=0)
            target = target.detach().cpu().numpy()
            target = np.array(list(map(lambda x: label_map[x], target)))
            #correct += np.sum(pred == target)

            # compute error/accuracy prediction accuracy
            cpu_output = np.array(cpu_output)
            cpu_output = np.where(cpu_output <= 0.5, 0, cpu_output)
            cpu_output = np.where(cpu_output > 0.5, 1, cpu_output)
            acc1_target = acc1.detach().cpu().numpy()
            acc2_target = acc2.detach().cpu().numpy()
            correct_in1 = cpu_output[0]
            correct_in2 = cpu_output[1]

            if criteria == 'combined':
                correct += np.sum(np.logical_and((pred == target), np.logical_and((correct_in1 == acc1_target), (correct_in2 == acc2_target))))
            elif criteria == 'rank':
                correct += np.sum(pred == target)
            elif criteria == 'error':
                if single_net:
                    correct += np.sum(correct_in1 == acc1_target)
                else:
                    correct += np.sum(np.logical_and((correct_in1 == acc1_target), (correct_in2 == acc2_target)))
            else:
                print('Unsuported evalation criteria')
                exit(0)

        num_objects_current += input_ids1.size(0)

        if verbose and 10 * (i + 1) / num_batches >= verb_stage + 1:
            print('Stage %d/10. Loss: %12.4f. Acc: %6.2f' % (
                verb_stage + 1, loss_sum / num_objects_current,
                correct / num_objects_current * 100.0
            ))
            verb_stage += 1

    res= {
        'loss': loss_sum / num_objects_current,
        'accuracy': None if regression else correct / num_objects_current * 100.0,
        'stats': {key: value / num_objects_current for key, value in stats_sum.items()}
    }
    return res


def eval(loader, model, criterion, regression=False, verbose=True, eval=True, criteria='combined', single_net=False):
    loss_sum = 0.0
    correct = 0.0
    stats_sum = defaultdict(float)
    num_objects_total = len(loader.dataset)
    label_map = {-1: 1, 1: 0}
    model.train(not eval)
    with torch.no_grad():
        if verbose:
            loader = tqdm.tqdm(loader)
        for _, batch in enumerate(loader):
            input_ids1 = batch["input_ids1"]
            attention_mask1 = batch["attention_mask1"]
            token_type_ids1 = batch["token_type_ids1"]

            input_ids2 = batch["input_ids2"]
            attention_mask2 = batch["attention_mask2"]
            token_type_ids2 = batch["token_type_ids2"]
            target = batch['targets']
            score1 = batch['score1']
            score2 = batch['score2']
            acc1 = batch['acc1']
            acc2 = batch['acc2']
            target_comb = (target, score1, score2, acc1, acc2)
            if torch.cuda.is_available():
                input_ids1 = input_ids1.cuda(non_blocking=True)
                attention_mask1 = attention_mask1.cuda(non_blocking=True)
                token_type_ids1 = token_type_ids1.cuda(non_blocking=True)

                input_ids2 = input_ids2.cuda(non_blocking=True)
                attention_mask2 = attention_mask2.cuda(non_blocking=True)
                token_type_ids2 = token_type_ids2.cuda(non_blocking=True)
                target = target.cuda(non_blocking=True)
                score1 = score1.cuda(non_blocking=True)
                score2 = score2.cuda(non_blocking=True)
                acc1 = acc1.cuda(non_blocking=True)
                acc2 = acc2.cuda(non_blocking=True)            

            loss, output, stats = criterion(model, target_comb, input_ids1=input_ids1, input_ids2=input_ids2,
                                        attention_mask1=attention_mask1, attention_mask2=attention_mask2,
                                        token_type_ids1=token_type_ids1, token_type_ids2=token_type_ids2)
        
            loss_sum += loss.item() * input_ids1.size(0)
            for key, value in stats.items():
                stats_sum[key] += value

            cpu_output = output.detach().cpu().numpy()
            pred = np.argmax(cpu_output, axis=0)
            target = target.detach().cpu().numpy()
            target = np.array(list(map(lambda x: label_map[x], target)))
            #correct += np.sum(pred == target)

            cpu_output = np.array(cpu_output)
            cpu_output = np.where(cpu_output <= 0.5, 0, cpu_output)
            cpu_output = np.where(cpu_output > 0.5, 1, cpu_output)
            acc1_target = acc1.detach().cpu().numpy()
            acc2_target = acc2.detach().cpu().numpy()
            correct_in1 = cpu_output[0]
            correct_in2 = cpu_output[1]

            if criteria == 'combined':
                correct += np.sum(np.logical_and((pred == target), np.logical_and((correct_in1 == acc1_target), (correct_in2 == acc2_target))))
            elif criteria == 'rank':
                correct += np.sum(pred == target)
            elif criteria == 'error':
                if single_net:
                    correct += np.sum(correct_in1 == acc1_target)
                else:
                    correct += np.sum(np.logical_and((correct_in1 == acc1_target), (correct_in2 == acc2_target)))
            else:
                print('Unsuported evalation criteria')
                exit(0)


    return {
        'loss': loss_sum / num_objects_total,
        'accuracy': None if regression else correct / num_objects_total * 100.0,
        'stats': {key: value / num_objects_total for key, value in stats_sum.items()}
    }


def predict(loader, model, eval=True, verbose=True):
    scores = list()
    if eval:
        model.eval()

    if verbose:
        loader = tqdm.tqdm(loader)

    with torch.no_grad():
        for batch in loader:
            input_ids = batch['input_ids1'].cuda(non_blocking=True)
            attention_mask = batch['attention_mask1'].cuda(non_blocking=True)
            token_type_ids = batch['token_type_ids1'].cuda(non_blocking=True)
            score, _ = model.forward_single_item(
                input_ids, attention_mask,token_type_ids)
            score = np.squeeze(score.detach().cpu().numpy())
            if len(score.shape) !=0:
                try:
                    scores.extend(score)
                except:
                    print(f'wrong score {score}')
                    print(f'wrong shape score {score.shape}')
                    print('something wrong happen')
                    exit()
            else:
                scores.append(score)
    return scores


def moving_average(net1, net2, alpha=1):
    for param1, param2 in zip(net1.parameters(), net2.parameters()):
        param1.data *= (1.0 - alpha)
        param1.data += param2.data * alpha


def _check_bn(module, flag):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        flag[0] = True


def check_bn(model):
    flag = [False]
    model.apply(lambda module: _check_bn(module, flag))
    return flag[0]


def reset_bn(module):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.running_mean = torch.zeros_like(module.running_mean)
        module.running_var = torch.ones_like(module.running_var)


def _get_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        momenta[module] = module.momentum


def _set_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.momentum = momenta[module]


def bn_update(loader, model, verbose=False, subset=None, **kwargs):
    """
        BatchNorm buffers update (if any).
        Performs 1 epochs to estimate buffers average using train dataset.

        :param loader: train dataset loader for buffers average estimation.
        :param model: model being update
        :return: None
    """
    if not check_bn(model):
        return
    model.train()
    momenta = {}
    model.apply(reset_bn)
    model.apply(lambda module: _get_momenta(module, momenta))
    n = 0
    num_batches = len(loader)

    with torch.no_grad():
        if subset is not None:
            num_batches = int(num_batches * subset)
            loader = itertools.islice(loader, num_batches)
        if verbose:
            loader = tqdm.tqdm(loader, total=num_batches)

        for input, _ in loader:
            input = input.cuda(non_blocking=True)
            input_var = torch.autograd.Variable(input)
            b = input_var.data.size(0)

            momentum = b / (n + b)
            for module in momenta.keys():
                module.momentum = momentum

            model(input_var, **kwargs)
            n += b

    model.apply(lambda module: _set_momenta(module, momenta))


def inv_softmax(x, eps=1e-10):
    return torch.log(x/(1.0 - x + eps))


def predictions(test_loader, model, seed=None, cuda=True, regression=False, **kwargs):
    # will assume that model is already in eval mode
    # model.eval()
    preds = []
    targets = []
    for input, target in test_loader:
        if seed is not None:
            torch.manual_seed(seed)
        if cuda:
            input = input.cuda(non_blocking=True)
        output = model(input, **kwargs)
        if regression:
            preds.append(output.cpu().data.numpy())
        else:
            probs = F.softmax(output, dim=1)
            preds.append(probs.cpu().data.numpy())
        targets.append(target.numpy())
    return np.vstack(preds), np.concatenate(targets)


def set_weights(model, vector, layers, device=None):
    offset = 0
    # for param in model.parameters():
    for name, param in model.named_parameters():
        for layer in layers:
            if layer in name:
                param.data.copy_(
                    vector[offset:offset + param.numel()].view(param.size()).to(device))
                offset += param.numel()


def extract_parameters(model):
    params = []
    for module in model.modules():
        for name in list(module._parameters.keys()):
            if module._parameters[name] is None:
                continue
            param = module._parameters[name]
            params.append((module, name, param.size()))
            module._parameters.pop(name)
    return params


def set_weights_old(params, w, device):
    offset = 0
    for module, name, shape in params:
        size = np.prod(shape)
        value = w[offset:offset + size]
        setattr(module, name, value.view(shape).to(device))
        offset += size


def nll(outputs, labels):
    labels = labels.astype(int)
    idx = (np.arange(labels.size), labels)
    ps = outputs[idx]
    nll = -np.sum(np.log(ps))
    return nll


def accuracy(outputs, labels):
    return (np.argmax(outputs, axis=1) == labels).mean()


def calibration_curve(outputs, labels, num_bins=20):
    confidences = np.max(outputs, 1)
    step = (confidences.shape[0] + num_bins - 1) // num_bins
    bins = np.sort(confidences)[::step]
    if confidences.shape[0] % step != 1:
        bins = np.concatenate((bins, [np.max(confidences)]))
    #bins = np.linspace(0.1, 1.0, 30)
    predictions = np.argmax(outputs, 1)
    bin_lowers = bins[:-1]
    bin_uppers = bins[1:]

    accuracies = predictions == labels

    xs = []
    ys = []
    zs = []

    #ece = Variable(torch.zeros(1)).type_as(confidences)
    ece = 0.0
    for bin_lower, bin_upper in zip(bin_lowers, bin_uppers):
        # Calculated |confidence - accuracy| in each bin
        in_bin = (confidences > bin_lower) * (confidences < bin_upper)
        prop_in_bin = in_bin.mean()
        if prop_in_bin > 0:
            accuracy_in_bin = accuracies[in_bin].mean()
            avg_confidence_in_bin = confidences[in_bin].mean()
            ece += np.abs(avg_confidence_in_bin-accuracy_in_bin) * prop_in_bin
            xs.append(avg_confidence_in_bin)
            ys.append(accuracy_in_bin)
            zs.append(prop_in_bin)
    xs = np.array(xs)
    ys = np.array(ys)
    zs = np.array(zs)

    out = {
        'confidence': xs,
        'accuracy': ys,
        'p': zs,
        'ece': ece,
    }
    return out


def schedule(epoch, swag_start, swag_lr, lr_init):
    t = epoch / swag_start
    lr_ratio = swag_lr / lr_init
    if t <= 0.5:
        factor = 1.0
    elif t <= 0.9:
        factor = 1.0 - (1.0 - lr_ratio) * (t - 0.5) / 0.4
    else:
        factor = lr_ratio
    return lr_init * factor


def ece(outputs, labels, num_bins=20):
    return calibration_curve(outputs, labels, num_bins=20)['ece']
