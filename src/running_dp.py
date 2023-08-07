import torch
import torch.nn as nn
import torch.utils.data as data
from tools import pass_forward, cal_set_difference_seq
from data import load_dataset, get_subdataset, get_dataloader
from adv_model import DiffPrvBackdoorRegistrar, InitEncoderMLP, EncoderMLP, modifygradsamplemodule
from opacus.validators import ModuleValidator
from opacus import PrivacyEngine
import torch.optim as optim
from train import dp_train_by_epoch, evaluation, train_model
import random


def find_available_bait_and_target(features, baits_candidate, sill=None, metric_func=None):
    # features: num_samples * num_entries, num_baits * num_entries
    scores = features @ baits_candidate.t()  # num_samples * num_baits
    scores_top2, image_indices = scores.topk(2, dim=0)  # topk * num_baits
    scores_first, scores_second = scores_top2[0], scores_top2[1]  # num_baits
    if metric_func is None:
        metric_func = lambda x, y: x - y

    metric = metric_func(scores_first, scores_second)
    is_satisfy = (metric > sill)

    target_img_indices = image_indices[0, is_satisfy]
    upperbound, lowerbound = scores_first[is_satisfy], scores_second[is_satisfy]
    return target_img_indices, baits_candidate[is_satisfy], (upperbound, lowerbound)


def find_self_consist(features, centralize_multiplier, target_img_indices):
    baits_candidate = features[target_img_indices]
    baits_candidate = baits_candidate - centralize_multiplier * baits_candidate.mean(dim=1, keepdim=True)
    baits_candidate = baits_candidate / baits_candidate.norm(dim=1, keepdim=True)
    scores = features @ baits_candidate.t() # num_samples, num_baits

    scores_targeted = scores[target_img_indices, torch.arange(len(target_img_indices))]
    scores_top2, image_indices = scores.topk(2, dim=0)  # topk * num_baits
    scores_first, scores_second = scores_top2[0], scores_top2[1]  # num_baits
    is_satisfy = (scores_targeted >= scores_first)

    upperbound, lowerbound = scores_first[is_satisfy], scores_second[is_satisfy]
    return target_img_indices[is_satisfy], baits_candidate[is_satisfy], (upperbound, lowerbound)


def target_sample_selector(model, dataset, num_target=1, approach='gaussian', approach_param=None):
    dl = data.DataLoader(dataset, batch_size=128, shuffle=False, num_workers=2)

    features, labels = pass_forward(model, dl, return_label=True)
    num_features = features.shape[1]

    if approach == 'gaussian':
        num_cast_bait, sill = approach_param['num_cast_bait'], approach_param['sill']
        if 'metric_func' in approach_param.keys():
            metric_func = eval(approach)
        else:
            metric_func = None

        baits_candidate = torch.randn(num_cast_bait, num_features)
        baits_candidate = baits_candidate / baits_candidate.norm(dim=1, keepdim=True)
        target_img_indices, baits_candidate, upperlowerbounds = find_available_bait_and_target(features, baits_candidate=baits_candidate, sill=sill, metric_func=metric_func)
    elif approach == 'self':
        centralize_multiplier, target_img_indices = approach_param['centralize_multiplier'], approach_param['target_img_indices']
        target_img_indices, baits_candidate, upperlowerbounds = find_self_consist(features, centralize_multiplier=centralize_multiplier, target_img_indices=target_img_indices)
        assert num_target <= len(target_img_indices), 'the match bait is not as many as needed'
    else:
        target_img_indices, baits_candidate, upperlowerbounds = None, None, None
    indices_selected = random.sample([j for j in range(len(target_img_indices))], num_target)
    target_img_indices, baits, upperlowerbounds = target_img_indices[indices_selected], baits_candidate[indices_selected], \
                                                            (upperlowerbounds[0][indices_selected], upperlowerbounds[1][indices_selected])

    return target_img_indices, baits, upperlowerbounds


def get_dataset_complement(dataset, target_img_indices):
    num_dataset = len(dataset)
    assert torch.all(torch.logical_and(target_img_indices < num_dataset, target_img_indices >=0)), 'WRONG INPUT INDICES'
    complement_indices = cal_set_difference_seq(n=len(dataset), indices=target_img_indices)
    dataset_left = data.Subset(dataset, complement_indices)
    return dataset_left


def make_toy_backdoor_tester(num_targets, encoder, baits, input_sizes=(3, 32, 32)):
    image = torch.rand(input_sizes)
    image = image.unsqueeze(dim=0)
    features = encoder(image)
    num_features = features.shape[1]
    backdoor = nn.Linear(num_features, num_targets)

    for j in range(num_targets):
        backdoor.weight.data[j] = baits[j]
        backdoor.bias.data[j] = 0.0

    toy_md = nn.Sequential(encoder, backdoor)
    return toy_md


def check_match(num_targets, encoder, targets_image_label, baits, upperlowerbounds): # use a small linear layer to do this
    largest = upperlowerbounds[0]
    toy_md = make_toy_backdoor_tester(num_targets=num_targets, encoder=encoder, baits=baits, input_sizes=targets_image_label[0][0].shape)

    signals = []
    with torch.no_grad():
        for j in range(num_targets):
            image, label = targets_image_label[j]
            image = image.unsqueeze(dim=0)
            signal = toy_md(image)
            signals.append(signal[0, j].unsqueeze(dim=0))
    signals = torch.cat(signals)
    return torch.norm(signals - largest)


def check_largest(num_targets, encoder, baits, upperlowerbounds, dataset):
    toy_md = make_toy_backdoor_tester(num_targets=num_targets, encoder=encoder, input_sizes=dataset[0][0].shape, baits=baits)
    dl = data.DataLoader(dataset, batch_size=128, shuffle=False, num_workers=2)
    features, labels = pass_forward(toy_md, dl, return_label=True)
    top2, _ = features.topk(2, dim=0)
    return top2[0] - upperlowerbounds[0], top2[1] - upperlowerbounds[1]


def set_threshold(upperlowerbounds, threshold_quantile=0.9, passing_threshold_quantile=0.1):
    assert threshold_quantile > passing_threshold_quantile, 'meaningless threshold quantiles may exist'
    upper_bound, lower_bound = upperlowerbounds[0], upperlowerbounds[1]
    threshold = threshold_quantile * upper_bound + (1 - threshold_quantile) * lower_bound
    passing_threshold = passing_threshold_quantile * upper_bound + (1 - passing_threshold_quantile) * lower_bound
    return threshold, passing_threshold


def path_decorator(save_path, suffix=''):
    if save_path[-4] == '.':
        assert save_path[-3:] == 'pth', 'WRONG saving suffix'
        prefix = save_path[:-4]
        wgt_save_path = f'{prefix}_wgt{suffix}.pth'
        rgs_save_path = f'{prefix}_rgs{suffix}.pth'
    elif save_path[-3] == '.':
        assert save_path[-2:] == 'pt', 'WRONG saving suffix'
        prefix = save_path[:-3]
        wgt_save_path = f'{prefix}_wgt{suffix}.pt'
        rgs_save_path = f'{prefix}_rgs{suffix}.pt'
    elif '.' not in save_path:
        wgt_save_path = f'{save_path}_wgt{suffix}.pth'
        rgs_save_path = f'{save_path}_rgs{suffix}.pth'
    else:
        wgt_save_path = save_path
        rgs_save_path = None
    return wgt_save_path, rgs_save_path


def build_public_model(info_dataset, info_model, info_train, logger, save_path=None):
    ds_name, ds_path = info_dataset['NAME'], info_dataset['ROOT']
    is_normalize_ds = info_dataset.get('IS_NORMALIZE', True)
    train_dataset, test_dataset, resolution, num_classes = load_dataset(ds_path, ds_name, is_normalize=is_normalize_ds, is_augment=True)

    # dp-sgd training - related hyper-parameters
    batch_size, learning_rate, num_epochs = info_train.get('BATCH_SIZE', 1024), info_train.get('LR', 0.1), info_train.get('EPOCHS', 10)
    device, num_workers, verbose = info_train.get('DEVICE', 'cpu'), info_train.get('NUM_WORKERS', 2), info_train.get('VERBOSE', False)

    # model architecture and initialization related hyper-parameters
    cnn_encoder_modules = info_model.get('CNN_ENCODER', None)
    cnn_encoder = nn.Sequential(*[eval('nn.' + cnn_module) for cnn_module in cnn_encoder_modules])
    mlp_sizes = info_model.get('MLP_SIZES', (256, 256))
    dropout = info_model.get('DROPOUT', None)
    classifier = EncoderMLP(cnn_encoder, mlp_sizes=mlp_sizes, input_size=(3, resolution, resolution),
                            num_classes=num_classes, dropout=dropout)
    classifier.activate_gradient_or_not(module='encoder', is_activate=True)
    classifier.activate_gradient_or_not(module='mlp', is_activate=True)
    optimizer = optim.SGD(classifier.parameters(), lr=learning_rate)
    train_loader, test_loader = get_dataloader(ds0=train_dataset, ds1=test_dataset, batch_size=batch_size,
                                               num_workers=num_workers)
    dataloaders = {'train': train_loader, 'val': test_loader}
    train_model(classifier, dataloaders, optimizer, num_epochs=num_epochs, device=device, verbose=verbose, logger=logger)
    classifier = classifier.to('cpu')

    if save_path is not None:
        torch.save(classifier.save_weight(), save_path)


def dp_train(num_epochs, classifier, train_loader, test_loader, optimizer, privacy_engine, delta=1e-5, device='cpu', max_physical_batch_size=64, logger=None):
    for epoch in range(num_epochs):
        classifier.backdoor_registrar.update_epoch(epoch)
        dp_train_by_epoch(classifier, train_loader, optimizer, privacy_engine, epoch=epoch, delta=delta, device=device,
                          max_physical_batch_size=max_physical_batch_size, logger=logger)
    classifier.update_state()
    test_acc = evaluation(classifier, test_loader, device=device)
    logger.info(f"\tTest set Acc: {test_acc:.4f}")


def build_dp_model(info_dataset, info_model, info_train, info_target, logger=None, save_path=None):
    # TODO: to make sure that the read data belongs to our hope, no so much default value
    # TODO: wrap the wapper of model to have the written interface
    # TODO: check the weight, whether we have set it correctly?
    # dataset-related hyper-parameters
    ds_name, ds_path = info_dataset['NAME'], info_dataset['ROOT']
    ds_train_subset, is_normalize_ds = info_dataset.get('SUBSET', None), info_dataset.get('IS_NORMALIZE', True)
    train_dataset, test_dataset, resolution, num_classes = load_dataset(ds_path, ds_name, is_normalize=is_normalize_ds)
    train_dataset, _ = get_subdataset(train_dataset, p=ds_train_subset)

    # dp-sgd training - related hyper-parameters
    batch_size, max_physical_batch_size = info_train['BATCH_SIZE'], info_train['MAX_PHYSICAL_BATCH_SIZE']   # training
    is_with_epsilon = info_train['IS_WITH_EPSILON']
    max_grad_norm, epsilon, delta, noise_multiplier = info_train['MAX_GRAD_NORM'], info_train.get('EPSILON', None), info_train['DELTA'], info_train.get('NOISE_MULTIPLIER', None)
    learning_rate, num_epochs = info_train['LR'], info_train['EPOCHS']
    device, num_workers = info_train['DEVICE'], info_train.get('NUM_WORKERS', 0)

    # model architecture and initialization related hyper-parameters
    cnn_encoder_modules, mlp_sizes = info_model['CNN_ENCODER'], info_model['MLP_SIZES']
    cnn_encoder = nn.Sequential(*[eval('nn.' + cnn_module) for cnn_module in cnn_encoder_modules])
    pretrain_path = info_model['PRETRAIN_PATH']
    info_backdoor = info_model['BACKDOOR']
    # backdoor related hyper-parameters
    num_bkd = info_backdoor['NUM_BKD']
    indices_bkd_u, indices_bkd_v = info_backdoor['IDX_SUBMODULE']['MLP_U'], info_backdoor['IDX_SUBMODULE']['MLP_V']
    encoder_scaling_module_idx = info_backdoor['IDX_SUBMODULE']['ENCODER']
    multipliers = info_backdoor['MULTIPLIERS']
    # bacdoor bait construction
    approach = info_backdoor['BAIT_CONSTRUCTION'].get('APPROACH', 'gaussian')
    approach_param = info_backdoor['BAIT_CONSTRUCTION'].get('APPROACH_PARAM', {})
    threshold_quantile, passing_threshold_quantile = info_backdoor['BAIT_CONSTRUCTION']['Q_THRESHOLD'], info_backdoor['BAIT_CONSTRUCTION']['Q_PASSING_THRESHOLD']

    # trial related hyper-parameters
    num_experiments = info_target.get('NUM_EXPERIMENTS', 1)
    has_membership = info_target['HAS_MEMBERSHIP']
    num_targets = info_target.get('NUM_TARGETS', 1)
    target_img_indices = info_target.get('TARGET_IMG_INDICES', None)
    if approach == 'self':
        approach_param['target_img_indices'] = target_img_indices
    logger.info(f'There are {num_bkd} backdoors and {num_targets} target images')

    cnn_encoder.load_state_dict(torch.load(pretrain_path, map_location='cpu')['encoder'])
    target_img_indices, baits, upperlowerbounds = target_sample_selector(cnn_encoder, dataset=train_dataset, num_target=num_targets, approach=approach, approach_param=approach_param)
    targets_image_label = [train_dataset[idx] for idx in target_img_indices]
    mismatch_metric = check_match(num_targets=num_targets, encoder=cnn_encoder, targets_image_label=targets_image_label,
                                  baits=baits, upperlowerbounds=upperlowerbounds)
    assert mismatch_metric < 1e-4, 'the image, bait, bound do NOT match'

    dataset_disappear = get_dataset_complement(dataset=train_dataset, target_img_indices=target_img_indices)
    train_loader_appear, test_loader = get_dataloader(ds0=train_dataset, ds1=test_dataset, batch_size=batch_size, num_workers=num_workers) # https://github.com/pyg-team/pytorch_geometric/issues/366
    train_loader_disappear = get_dataloader(ds0=dataset_disappear, batch_size=batch_size, num_workers=num_workers)

    threshold, passing_threshold = set_threshold(upperlowerbounds, threshold_quantile=threshold_quantile, passing_threshold_quantile=passing_threshold_quantile)
    initialization_information = {'encoder_scaling_module_idx': encoder_scaling_module_idx, 'baits':baits, 'thresholds':threshold,
                                  'passing_threshold': passing_threshold, 'multipliers':multipliers}
    logger.info(f'upper bounds:{upperlowerbounds[0]}\n lower bounds:{upperlowerbounds[1]}\n threshold:{threshold}\n passing threshold:{passing_threshold}')
    logger.info(f'multipliers:{multipliers}')

    classifier = InitEncoderMLP(encoder=cnn_encoder, mlp_sizes=mlp_sizes, input_size=(3, resolution, resolution),
                                    num_classes=num_classes)
    classifier.load_weight(pretrain_path, which_module='encoder')
    errors = ModuleValidator.validate(classifier, strict=False)
    logger.info(errors)

    optimizer = optim.SGD(classifier.module_parameters(module='mlp'), lr=learning_rate)

    for j in range(num_experiments):  # TODO: control times for the training and random number related to it
        logger.info(f'EXPERIMENTS {j}:')
        if isinstance(has_membership, bool):
            has_membership_this_experiment = has_membership
        else:
            assert isinstance(has_membership, float), 'the has membership can only be float and bool'
            assert has_membership >= 0. and has_membership <= 1., 'the probability is wrong'
            rv = torch.rand(1).item()
            has_membership_this_experiment = True if rv < has_membership else False
        train_loader = train_loader_appear if has_membership_this_experiment else train_loader_disappear
        logger.info(f'HAS MEMBERSHIP? {has_membership_this_experiment}, length of {len(train_loader.dataset)}')

        backdoor_registrar = DiffPrvBackdoorRegistrar(num_bkd=num_bkd, indices_bkd_u=indices_bkd_u,indices_bkd_v=indices_bkd_v,
                                                      m_u=mlp_sizes[0], m_v=mlp_sizes[1], target_image_label=targets_image_label)
        classifier.vanilla_initialize(**initialization_information, backdoor_registrar=backdoor_registrar)
        privacy_engine = PrivacyEngine()
        if is_with_epsilon:
            safe_classifier, safe_optimizer, safe_train_loader = privacy_engine.make_private_with_epsilon(module=classifier, optimizer=optimizer, data_loader=train_loader,
                                                                                       epochs=num_epochs, target_epsilon=epsilon, target_delta=delta, max_grad_norm=max_grad_norm)
        else:
            safe_classifier, safe_optimizer, safe_train_loader = privacy_engine.make_private(module=classifier, optimizer=optimizer, data_loader=train_loader,
                                                                          noise_multiplier=noise_multiplier, max_grad_norm=max_grad_norm)

        modifygradsamplemodule(safe_classifier, backdoor_registrar=backdoor_registrar)

        logger.info(f"Using sigma={safe_optimizer.noise_multiplier}, C={max_grad_norm}, Epochs={num_epochs}")
        logger.info('NOW WE HAVE FINISHED INITIALIZATION, STARTING TRAINING!!!')
        dp_train(num_epochs, safe_classifier, safe_train_loader, test_loader, safe_optimizer, privacy_engine=privacy_engine, delta=delta, device=device,
                 max_physical_batch_size=max_physical_batch_size, logger=logger)

        safe_classifier = safe_classifier.to('cpu')
        if save_path is not None:
            wgt_save_path, rgs_save_path = path_decorator(save_path, f'_ex{j}')
            # TODO: check id
            torch.save(classifier.save_weight(), wgt_save_path)
            torch.save(safe_classifier.backdoor_registrar, rgs_save_path)


if __name__ == '__main__':
    ds_path = '../../cifar10'
    md_path = './weights/pretr_cifar100.pth'
    md_weights = torch.load(md_path, map_location='cpu')
    num_targets = 10
    cnn_encoder = nn.Sequential(nn.Conv2d(3, 64, kernel_size=5, stride=2, padding=2), nn.ReLU(),
                                 nn.MaxPool2d(kernel_size=3, stride=2, padding=1), nn.Conv2d(64, 64, kernel_size=3, stride=2, padding=1),
                                 nn.ReLU(), nn.MaxPool2d(kernel_size=3, stride=2, padding=1), nn.Flatten())
    cnn_encoder.load_state_dict(md_weights['encoder'])
    tr_ds, test_ds, resolution, classes = load_dataset(ds_path, 'cifar10', is_normalize=True)
    targets_image_label, baits, upperlowerbounds = target_sample_selector(cnn_encoder, dataset=tr_ds, num_target=num_targets, approach='gaussian', approach_param={'num_cast_bait': 1000, 'sill': 5.0})
    # eps_hat = check_match(num_targets=num_targets, encoder=cnn_encoder, targets_image_label=targets_image_label, baits=baits, upperlowerbounds=upperlowerbounds)
    # print(eps_hat)
    top1_diff, top2_diff = check_largest(num_targets=num_targets, encoder=cnn_encoder, baits=baits, upperlowerbounds=upperlowerbounds, dataset=tr_ds)
    print(top1_diff, top2_diff)

