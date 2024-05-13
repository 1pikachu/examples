import argparse
import os
import random
import shutil
import time
import warnings
from enum import Enum
import PIL
from context_func import context_func

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.datasets as datasets
import torchvision.models as models
import torchvision.transforms as transforms
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import Subset

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))
model_names += ["fbnetc_100", "spnasnet_100"]

params_dict = {
    # Coefficients:   width,depth,res,dropout
    'efficientnet_b0': (1.0, 1.0, 224, 0.2),
    'efficientnet_b1': (1.0, 1.1, 240, 0.2),
    'efficientnet_b2': (1.1, 1.2, 260, 0.3),
    'efficientnet_b3': (1.2, 1.4, 300, 0.3),
    'efficientnet_b4': (1.4, 1.8, 380, 0.4),
    'efficientnet_b5': (1.6, 2.2, 456, 0.4),
    'efficientnet_b6': (1.8, 2.6, 528, 0.5),
    'efficientnet_b7': (2.0, 3.1, 600, 0.5),
    'efficientnet_b8': (2.2, 3.6, 672, 0.5),
    'efficientnet_l2': (4.3, 5.3, 800, 0.5),
}

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('--data', metavar='DIR', default='imagenet',
                    help='path to dataset (default: imagenet)')
parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)',
                    dest='weight_decay')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--multiprocessing-distributed', action='store_true',
                    help='Use multi-processing distributed training to launch '
                         'N processes per node, which has N GPUs. This is the '
                         'fastest way to use PyTorch for either single node or '
                         'multi node data parallel training')
parser.add_argument('--dummy', action='store_true', help="use fake data to benchmark")
# OOB
parser.add_argument('--num-classes', type=int, default=1000,
                    help='Number classes in dataset')
parser.add_argument('--precision', default="float32", type=str, help='precision')
parser.add_argument('--channels_last', default=1, type=int, help='Use NHWC or not')
parser.add_argument('--jit', action='store_true', default=False, help='enable JIT')
parser.add_argument('--trt', action='store_true', default=False,
                    help='enable fwk+trt')
parser.add_argument('--profile', action='store_true', default=False, help='collect timeline')
parser.add_argument('--num_iter', default=200, type=int, help='test iterations')
parser.add_argument('--num_warmup', default=20, type=int, help='test warmup')
parser.add_argument('--device', default='cpu', type=str, help='cpu, cuda or xpu')
parser.add_argument('--nv_fuser', action='store_true', default=False, help='enable nv fuser')
parser.add_argument('--bn_folding', action='store_true', default=False,
                    help='enable bn folding')
parser.add_argument('--image_size', default=224, type=int,
                    help='image size')
parser.add_argument('--compile', action='store_true', default=False, help='compile model')
parser.add_argument('--backend', default="inductor", type=str, help='backend')

args = parser.parse_args()
best_acc1 = 0


def main():

    if args.device == "xpu":
        import intel_extension_for_pytorch
    elif args.device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = False

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        cudnn.benchmark = False
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    if torch.cuda.is_available():
        ngpus_per_node = torch.cuda.device_count()
    else:
        ngpus_per_node = 1
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)


def main_worker(gpu, ngpus_per_node, args):
    global best_acc1
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
    # create model
    if 'efficientnet_b8' in args.arch:  # NEW
        import geffnet
        if args.jit:
            geffnet.config.set_scriptable(True)
        if args.pretrained:
            model = geffnet.create_model(args.arch, num_classes=args.num_classes, in_chans=3, pretrained=True)
            print("=> using pre-trained model '{}'".format(args.arch))
        else:
            print("=> creating model '{}'".format(args.arch))
            model = geffnet.create_model(args.arch, num_classes=args.num_classes, in_chans=3, pretrained=False)
    elif 'mixnet' in args.arch or 'fbnetc_100' in args.arch or 'spnasnet_100' in args.arch:
        import geffnet
        if args.jit:
            geffnet.config.set_scriptable(True)
        if args.pretrained:
            model = geffnet.create_model(args.arch, num_classes=args.num_classes, in_chans=3, pretrained=True)
            print("=> using pre-trained model '{}'".format(args.arch))
        else:
            print("=> creating model '{}'".format(args.arch))
            model = geffnet.create_model(args.arch, num_classes=args.num_classes, in_chans=3, pretrained=False)

    else:
        if args.pretrained:
            print("=> using pre-trained model '{}'".format(args.arch))
            if args.arch == "inception_v3":
                model = models.__dict__[args.arch](pretrained=True, aux_logits=True, transform_input=False)
            else:
                if args.arch == "googlenet":
                    model = models.__dict__[args.arch](pretrained=True, transform_input=False)
                elif args.arch == "squeezenet1_1":
                    model = models.squeezenet1_1(weights='SqueezeNet1_1_Weights.DEFAULT')
                elif args.arch == "squeezenet1_0":
                    model = models.squeezenet1_0(weights='SqueezeNet1_0_Weights.DEFAULT')
                else:
                    model = models.__dict__[args.arch](pretrained=True)
        else:
            if args.arch == "inception_v3":
                print("=> creating model '{}'".format(args.arch))
                model = models.__dict__[args.arch](aux_logits=True)
            else:
                print("=> creating model '{}'".format(args.arch))
                model = models.__dict__[args.arch]()

    #if not torch.cuda.is_available() and not torch.backends.mps.is_available():
    #    device = torch.device(args.device)
    #    model = model.to(device)
    #elif args.distributed:
    #    # For multiprocessing distributed, DistributedDataParallel constructor
    #    # should always set the single device scope, otherwise,
    #    # DistributedDataParallel will use all available devices.
    #    if torch.cuda.is_available():
    #        if args.gpu is not None:
    #            torch.cuda.set_device(args.gpu)
    #            model.cuda(args.gpu)
    #            # When using a single GPU per process and per
    #            # DistributedDataParallel, we need to divide the batch size
    #            # ourselves based on the total number of GPUs of the current node.
    #            args.batch_size = int(args.batch_size / ngpus_per_node)
    #            args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
    #            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
    #        else:
    #            model.cuda()
    #            # DistributedDataParallel will divide and allocate batch_size to all
    #            # available GPUs if device_ids are not set
    #            model = torch.nn.parallel.DistributedDataParallel(model)
    #elif args.gpu is not None and torch.cuda.is_available():
    #    torch.cuda.set_device(args.gpu)
    #    model = model.cuda(args.gpu)
    #elif torch.backends.mps.is_available():
    #    device = torch.device("mps")
    #    model = model.to(device)
    #else:
    #    # DataParallel will divide and allocate batch_size to all available GPUs
    #    if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
    #        model.features = torch.nn.DataParallel(model.features)
    #        model.cuda()
    #    else:
    #        model = torch.nn.DataParallel(model).cuda()
    device = torch.device(args.device)
    model = model.to(device)
    print("----model device:", device)
    if args.channels_last and args.device != "xpu":
        model = model.to(memory_format=torch.channels_last)
        print("Use NHWC model.")

    #if torch.cuda.is_available():
    #    if args.gpu:
    #        device = torch.device('cuda:{}'.format(args.gpu))
    #    else:
    #        device = torch.device("cuda")
    #elif torch.backends.mps.is_available():
    #    device = torch.device("mps")
    #else:
    #    device = torch.device(args.device)
    # define loss function (criterion), optimizer, and learning rate scheduler
    criterion = nn.CrossEntropyLoss().to(device)

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    args.datatype = torch.float16 if args.precision == "float16" else torch.bfloat16 if args.precision == "bfloat16" else torch.float
    if args.device == "xpu":
        if args.evaluate:
            model.eval()
            model = torch.xpu.optimize(model=model, dtype=args.datatype)
        else:
            model, optimizer = torch.xpu.optimize(model=model, optimizer=optimizer, dtype=args.datatype)
        print("----xpu optimize")
    if args.compile:
        print("----enable compiler")
        model = torch.compile(model, backend=args.backend, options={"freezing": True})
    
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    scheduler = StepLR(optimizer, step_size=30, gamma=0.1)
    
    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            if args.gpu is None:
                checkpoint = torch.load(args.resume)
            elif torch.cuda.is_available():
                # Map model to be loaded to specified single gpu.
                loc = 'cuda:{}'.format(args.gpu)
                checkpoint = torch.load(args.resume, map_location=loc)
            args.start_epoch = checkpoint['epoch']
            best_acc1 = checkpoint['best_acc1']
            if args.gpu is not None:
                # best_acc1 may be from a checkpoint from a different GPU
                best_acc1 = best_acc1.to(args.gpu)
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            scheduler.load_state_dict(checkpoint['scheduler'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])
    if 'efficientnet' in args.arch:
        image_size = get_image_size(args.arch)
        val_transforms = transforms.Compose([
            transforms.Resize(image_size, interpolation=PIL.Image.BICUBIC),
            transforms.CenterCrop(image_size),
            transforms.ToTensor(),
            normalize,
        ])
        args.image_size = image_size
        print('Using image size', image_size)
    elif 'mixnet' in args.arch:
        image_size = 112
        args.image_size = image_size
        print('Using image size', image_size)
    else:
        val_transforms = transforms.Compose([
            transforms.Resize(args.image_size),
            transforms.CenterCrop(args.image_size),
            transforms.ToTensor(),
            normalize,
        ])
        print('Using image size', args.image_size)

    # Data loading code
    if args.dummy:
        print("=> Dummy data is used!")
        train_dataset = datasets.FakeData(1281167, (3, args.image_size, args.image_size), 1000, transforms.ToTensor())
        val_dataset = datasets.FakeData(50000, (3, args.image_size, args.image_size), 1000, transforms.ToTensor())
    else:
        traindir = os.path.join(args.data, 'train')
        valdir = os.path.join(args.data, 'val')

        train_dataset = datasets.ImageFolder(
            traindir,
            transforms.Compose([
                transforms.RandomResizedCrop(224),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                normalize,
            ]))

        val_dataset = datasets.ImageFolder(
            valdir,
            val_transforms
            )

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset, shuffle=False, drop_last=True)
    else:
        train_sampler = None
        val_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)

    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, sampler=val_sampler)

    if args.evaluate:
        if args.device == "cuda" and args.trt:
            import torch_tensorrt
            script_model = torch.jit.script(model)

            images = torch.randn(args.batch_size, 3, args.image_size, args.image_size).cuda(args.gpu, non_blocking=True)
            if args.precision == "float16":
                images = images.half()
                enabled_precisions = {torch.half}
            else:
                enabled_precisions = {torch.float}
            spec = {
                #"inputs": [torch_tensorrt.Input([args.batch_size, 3, args.image_size, args.image_size], dtype=torch.half)],
                "inputs": [images],
                "enabled_precisions": enabled_precisions,
                "refit": False,
                "debug": False,
                "device": {
                    "device_type": torch_tensorrt.DeviceType.GPU,
                    "gpu_id": 0,
                    "dla_core": 0,
                    "allow_gpu_fallback": True
                },
                "capability": torch_tensorrt.EngineCapability.default,
                "num_min_timing_iters": 2,
                "num_avg_timing_iters": 1,
            }
            # trt_model = torch._C._jit_to_backend("tensorrt", script_model, spec)
            trt_model = torch_tensorrt.ts.compile(script_model, **spec)
            model = trt_model
            print("---- Use TRT model")

        with torch.autocast(enabled=True, device_type=args.device, dtype=args.datatype):
            validate(val_loader, model, criterion, args)
        return

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)

        # train for one epoch
        with torch.autocast(enabled=True, device_type=args.device, dtype=args.datatype):
            train(train_loader, model, criterion, optimizer, epoch, device, args)
        return

        # evaluate on validation set
        acc1 = validate(val_loader, model, criterion, args)
        
        scheduler.step()
        
        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)

        if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                and args.rank % ngpus_per_node == 0):
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_acc1': best_acc1,
                'optimizer' : optimizer.state_dict(),
                'scheduler' : scheduler.state_dict()
            }, is_best)


def train(train_loader, model, criterion, optimizer, epoch, device, args):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    profile_iter = (args.num_iter+args.num_warmup) // 2
    for i, (images, target) in enumerate(train_loader):
        if i == args.num_iter:
            break

        with context_func(args.profile and i == profile_iter, args.device, "none"):
            start_time = time.time()
            # move data to the same device as model
            images = images.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)

            # compute output
            with torch.xpu.amp.autocast(enabled=True, dtype=args.datatype):
                output = model(images)
                loss = criterion(output, target)
            # measure accuracy and record loss
            acc1, acc5 = accuracy(output, target, topk=(1, 5))
            losses.update(loss.item(), images.size(0))
            top1.update(acc1[0], images.size(0))
            top5.update(acc5[0], images.size(0))

            # compute gradient and do SGD step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # D2H
            loss.cpu()
            output.cpu()
            if args.device == "xpu":
                torch.xpu.synchronize()
            elif args.device == "cuda":
                torch.cuda.synchronize()
            duration = time.time() - start_time
        print("Iteration: {}, training time: {} sec.".format(i, duration), flush=True)
        # measure elapsed time 
        if i >= args.num_warmup:
            batch_time.update(duration)
        if i % args.print_freq == 0:
            progress.display(i + 1)

    batch_size = train_loader.batch_size
    latency = batch_time.avg / batch_size * 1000
    perf = batch_size/batch_time.avg
    print('%d epoch training latency: %3.0f ms'%(0, latency))
    print('%d epoch training Throughput: %3.0f fps'%(0, perf))

def validate(val_loader, model, criterion, args):
    if args.nv_fuser:
        fuser_mode = "fuser2"
    else:
        fuser_mode = "none"
    print("---- fuser mode:", fuser_mode)
    if args.jit:
        sample_input = iter(val_loader).__next__()[0].to(args.device)
        with torch.no_grad():
            try:
                modelJit = torch.jit.trace(model, sample_input, check_trace=False)
                print("---- Use trace model.")
                model = modelJit
            except (RuntimeError, TypeError) as e:
                print("---- JIT trace disable.")
                print("failed to use PyTorch jit mode due to: ", e)
            if args.bn_folding and args.device == "cuda":
                model = wrap_cpp_module(torch._C._jit_pass_fold_convbn(model._c))
                print("---- Conv+bn folding")

    def run_validate(loader, base_progress=0):
        profile_iter = (args.num_iter+args.num_warmup) // 2
        with torch.no_grad():
            for i, (images, target) in enumerate(loader):
                if i == args.num_iter:
                    break
                if args.channels_last and args.device != "xpu":
                    if len(images.shape) == 4:
                        images = images.to(memory_format=torch.channels_last)
                    elif len(images.shape) == 5:
                        images = images.to(memory_format=torch.channels_last_3d)

                with context_func(args.profile and i == profile_iter, args.device, fuser_mode):
                    start_time = time.time()
                    images = images.to(args.device)

                    # compute output
                    if args.device == "cuda":
                        with torch.jit.fuser(fuser_mode):
                            output = model(images)
                    else:
                        output = model(images)

                    if args.device == "cuda":
                        torch.cuda.synchronize()
                    elif args.device == "xpu":
                        torch.xpu.synchronize()
                    duration = time.time() - start_time
                print("Iteration: {}, inference time: {} sec.".format(i, duration), flush=True)
                if i >= args.num_warmup:
                    batch_time.update(duration)

            batch_size = args.batch_size
            latency = batch_time.avg / batch_size * 1000
            perf = batch_size/batch_time.avg
            print('inference latency: %3.3f ms'%latency)
            print('inference Throughput: %3.3f fps'%perf)

    batch_time = AverageMeter('Time', ':6.3f', Summary.NONE)
    losses = AverageMeter('Loss', ':.4e', Summary.NONE)
    top1 = AverageMeter('Acc@1', ':6.2f', Summary.AVERAGE)
    top5 = AverageMeter('Acc@5', ':6.2f', Summary.AVERAGE)

    # switch to evaluate mode
    model.eval()

    run_validate(val_loader)
    if args.distributed:
        top1.all_reduce()
        top5.all_reduce()

    if args.distributed and (len(val_loader.sampler) * args.world_size < len(val_loader.dataset)):
        aux_val_dataset = Subset(val_loader.dataset,
                                 range(len(val_loader.sampler) * args.world_size, len(val_loader.dataset)))
        aux_val_loader = torch.utils.data.DataLoader(
            aux_val_dataset, batch_size=args.batch_size, shuffle=False,
            num_workers=args.workers, pin_memory=True)
        run_validate(aux_val_loader, len(val_loader))

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')

class Summary(Enum):
    NONE = 0
    AVERAGE = 1
    SUM = 2
    COUNT = 3

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f', summary_type=Summary.AVERAGE):
        self.name = name
        self.fmt = fmt
        self.summary_type = summary_type
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def all_reduce(self):
        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
        total = torch.tensor([self.sum, self.count], dtype=torch.float32, device=device)
        dist.all_reduce(total, dist.ReduceOp.SUM, async_op=False)
        self.sum, self.count = total.tolist()
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)
    
    def summary(self):
        fmtstr = ''
        if self.summary_type is Summary.NONE:
            fmtstr = ''
        elif self.summary_type is Summary.AVERAGE:
            fmtstr = '{name} {avg:.3f}'
        elif self.summary_type is Summary.SUM:
            fmtstr = '{name} {sum:.3f}'
        elif self.summary_type is Summary.COUNT:
            fmtstr = '{name} {count:.3f}'
        else:
            raise ValueError('invalid summary type %r' % self.summary_type)
        
        return fmtstr.format(**self.__dict__)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print('\t'.join(entries))
        
    def display_summary(self):
        entries = [" *"]
        entries += [meter.summary() for meter in self.meters]
        print(' '.join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = '{:' + str(num_digits) + 'd}'
        return '[' + fmt + '/' + fmt.format(num_batches) + ']'

def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res

def get_image_size(model_name):
    if model_name in params_dict:
        _, _, res, _ = params_dict[model_name]
    else:
        assert False, "Unsupported model:{}".format(model_name)
    return res

def trace_handler(p):
    output = p.key_averages().table(sort_by="self_cpu_time_total")
    print(output)
    import pathlib
    timeline_dir = str(pathlib.Path.cwd()) + '/timeline/'
    if not os.path.exists(timeline_dir):
        try:
            os.makedirs(timeline_dir)
        except:
            pass
    timeline_file = timeline_dir + 'timeline-' + str(torch.backends.quantized.engine) + '-' + \
                args.arch + '-' + str(p.step_num) + '-' + str(os.getpid()) + '.json'
    p.export_chrome_trace(timeline_file)

if __name__ == '__main__':
    main()
