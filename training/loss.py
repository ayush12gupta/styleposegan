﻿# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import numpy as np
import torch
import torchvision
from torch_utils import training_stats
from torch_utils import misc
from torch_utils.ops import conv2d_gradfix

#----------------------------------------------------------------------------

class VGGPerceptualLoss(torch.nn.Module):
    def __init__(self, resize=True):
        super(VGGPerceptualLoss, self).__init__()
        blocks = []
        blocks.append(torchvision.models.vgg16(pretrained=True).cuda().features[:4].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).cuda().features[4:9].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).cuda().features[9:16].eval())
        blocks.append(torchvision.models.vgg16(pretrained=True).cuda().features[16:23].eval())
        for bl in blocks:
            for p in bl.parameters():
                p.requires_grad = False
        self.blocks = torch.nn.ModuleList(blocks)
        self.transform = torch.nn.functional.interpolate
        self.resize = resize
        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).cuda().view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).cuda().view(1, 3, 1, 1))

    def forward(self, input, target, feature_layers=[0, 1, 2, 3], style_layers=[]):
        if input.shape[1] != 3:
            input = input.repeat(1, 3, 1, 1)
            target = target.repeat(1, 3, 1, 1)
        input = (input-self.mean) / self.std
        target = (target-self.mean) / self.std
        if self.resize:
            input = self.transform(input, mode='bilinear', size=(224, 224), align_corners=False)
            target = self.transform(target, mode='bilinear', size=(224, 224), align_corners=False)
        loss = 0.0
        x = input
        y = target
        for i, block in enumerate(self.blocks):
            x = block(x)
            y = block(y)
            if i in feature_layers:
                loss += torch.nn.functional.l1_loss(x, y)
            if i in style_layers:
                act_x = x.reshape(x.shape[0], x.shape[1], -1)
                act_y = y.reshape(y.shape[0], y.shape[1], -1)
                gram_x = act_x @ act_x.permute(0, 2, 1)
                gram_y = act_y @ act_y.permute(0, 2, 1)
                loss += torch.nn.functional.l1_loss(gram_x, gram_y)
        return loss

#----------------------------------------------------------------------------

class Loss:
    def accumulate_gradients(self, phase, img_s, img_t, ap_s, ap_t, pose_s, pose_t, real_c, gen_c, sync, gain): # to be overridden by subclass
        raise NotImplementedError()

#----------------------------------------------------------------------------

class StyleGAN2Loss(Loss):
    def __init__(self, device, G_mapping, G_synthesis, ANet, D, augment_pipe=None, style_mixing_prob=0.9, r1_gamma=10, pl_batch_shrink=2, pl_decay=0.01, pl_weight=2, l1_weight=50):
        super().__init__()
        self.device = device
        self.G_mapping = G_mapping
        self.G_synthesis = G_synthesis
        self.D = D
        self.ANet = ANet
        self.augment_pipe = augment_pipe
        self.style_mixing_prob = style_mixing_prob
        self.r1_gamma = r1_gamma
        self.pl_batch_shrink = pl_batch_shrink
        self.pl_decay = pl_decay
        self.pl_weight = pl_weight
        self.vgg_loss = VGGPerceptualLoss()
        self.pl_mean = torch.zeros([], device=device)
        self.l1_weight = l1_weight

    def run_G(self, P, A, c, sync):
        with misc.ddp_sync(self.G_mapping, sync):
            z = self.ANet(A)
            ws = self.G_mapping(z, c)
            if self.style_mixing_prob > 0:
                with torch.autograd.profiler.record_function('style_mixing'):
                    cutoff = torch.empty([], dtype=torch.int64, device=ws.device).random_(1, ws.shape[1])
                    cutoff = torch.where(torch.rand([], device=ws.device) < self.style_mixing_prob, cutoff, torch.full_like(cutoff, ws.shape[1]))
                    ws[:, cutoff:] = self.G_mapping(torch.randn_like(z), c, skip_w_avg_update=True)[:, cutoff:]
        with misc.ddp_sync(self.G_synthesis, sync):
            img = self.G_synthesis(ws, P)
        return img, ws

    def run_D(self, img, c, sync):
        if self.augment_pipe is not None:
            img = self.augment_pipe(img)
        with misc.ddp_sync(self.D, sync):
            logits = self.D(img, c)
        return logits

    def accumulate_gradients(self, phase, img_s, img_t, ap_s, ap_t, pose_s, pose_t, real_c, gen_c, sync, gain):
        assert phase in ['Gmain', 'Greg', 'Gboth', 'Dmain', 'Dreg', 'Dboth']
        do_Gmain = (phase in ['Gmain', 'Gboth'])
        do_Dmain = (phase in ['Dmain', 'Dboth'])
        do_Dr1   = (phase in ['Dreg', 'Dboth']) and (self.r1_gamma != 0)
        do_GPercep = (phase in ['Gmain', 'Gboth'])

        loss_l1 = loss_vgg = loss_Dgen = loss_Gmain = loss_Dreal = None
        # Gmain: Maximize logits for generated images.
        if do_GPercep:
            with torch.autograd.profiler.record_function('Gmain_forward'):
                gen_img_s, _gen_ws = self.run_G(pose_s, ap_s, gen_c, sync=(sync)) # May get synced by Gpl.
                gen_img_t, _gen_ws = self.run_G(pose_t, ap_s, gen_c, sync=(sync)) # May get synced by Gpl.
                loss_l1_s = abs(torch.nn.functional.l1_loss(img_s, gen_img_s))#*l1_weight
                loss_l1_t = abs(torch.nn.functional.l1_loss(img_t, gen_img_t))#*l1_weight
                loss_l1 = loss_l1_s + loss_l1_t
                loss_vgg = self.vgg_loss(img_s, gen_img_s) + self.vgg_loss(img_s, gen_img_s)
                training_stats.report('Loss/G/L1_loss', loss_l1)
                training_stats.report('Loss/G/Perceptual', loss_vgg)
                # training_stats.report('Loss/G/loss', loss_Gmain)
            with torch.autograd.profiler.record_function('Gmain_backward'):
                (loss_l1 + loss_vgg).mean().mul(gain).backward()

        if do_Gmain:
            with torch.autograd.profiler.record_function('Gmain_forward'):
                gen_img_s, _gen_ws = self.run_G(pose_s, ap_s, gen_c, sync=(sync)) # May get synced by Gpl.
                gen_img_t, _gen_ws = self.run_G(pose_t, ap_s, gen_c, sync=(sync)) # May get synced by Gpl.
                gen_logits_s = self.run_D(gen_img_s, gen_c, sync=False)
                gen_logits_t = self.run_D(gen_img_t, gen_c, sync=False)
                gen_logits = gen_logits_s + gen_logits_t
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Gmain = torch.nn.functional.softplus(-gen_logits) # -log(sigmoid(gen_logits))
                training_stats.report('Loss/G/loss', loss_Gmain)
            with torch.autograd.profiler.record_function('Gmain_backward'):
                loss_Gmain.mean().mul(gain).backward()

        # Dmain: Minimize logits for generated images.
        
        if do_Dmain:
            loss_Dgen = 0
            with torch.autograd.profiler.record_function('Dgen_forward'):
                gen_img_s, _gen_ws = self.run_G(pose_s, ap_s, gen_c, sync=False)
                gen_logits = self.run_D(gen_img_s, gen_c, sync=False) # Gets synced by loss_Dreal.
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Dgen = torch.nn.functional.softplus(gen_logits) # -log(1 - sigmoid(gen_logits))
            with torch.autograd.profiler.record_function('Dgen_backward'):
                loss_Dgen.mean().mul(gain).backward()

        # Dmain: Maximize logits for real images.
        # Dr1: Apply R1 regularization.
        if do_Dmain or do_Dr1:
            name = 'Dreal_Dr1' if do_Dmain and do_Dr1 else 'Dreal' if do_Dmain else 'Dr1'
            with torch.autograd.profiler.record_function(name + '_forward'):
                real_img_s_tmp = img_s.detach().requires_grad_(do_Dr1)
                real_img_t_tmp = img_t.detach().requires_grad_(do_Dr1)
                real_s_logits = self.run_D(real_img_s_tmp, real_c, sync=sync)
                real_t_logits = self.run_D(real_img_t_tmp, real_c, sync=sync)
                real_logits = real_t_logits + real_s_logits
                training_stats.report('Loss/scores/real', real_logits)
                training_stats.report('Loss/signs/real', real_logits.sign())

                if do_Dmain:
                    loss_Dreal = 0
                    loss_Dreal = torch.nn.functional.softplus(-real_logits) # -log(sigmoid(real_logits))
                    training_stats.report('Loss/D/loss', loss_Dgen + loss_Dreal)

                loss_Dr1 = 0
                if do_Dr1:
                    with torch.autograd.profiler.record_function('r1_grads'), conv2d_gradfix.no_weight_gradients():
                        r1_grads_t = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_t_tmp], create_graph=True, only_inputs=True)[0]
                        r1_grads_s = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_s_tmp], create_graph=True, only_inputs=True)[0]
                    r1_grads = r1_grads_t + r1_grads_s
                    r1_penalty = r1_grads.square().sum([1,2,3])
                    loss_Dr1 = r1_penalty * (self.r1_gamma / 2)
                    training_stats.report('Loss/r1_penalty', r1_penalty)
                    training_stats.report('Loss/D/reg', loss_Dr1)

            with torch.autograd.profiler.record_function(name + '_backward'):
                if do_Dmain and do_Dr1:
                    (real_logits * 0 + loss_Dreal + loss_Dr1).mean().mul(gain).backward()
                elif do_Dr1:
                    (real_logits * 0 + loss_Dr1).mean().mul(gain).backward()
                else:
                    (real_logits * 0 + loss_Dreal).mean().mul(gain).backward()

        if loss_l1 is None:
            loss_l1 = torch.Tensor([0]).cuda()
        if loss_vgg is None:
            loss_vgg = torch.Tensor([0]).cuda()
        if loss_Gmain is None:
            loss_Gmain = torch.Tensor([0]).cuda()
        if loss_Dgen is None:
            loss_Dgen = torch.Tensor([0]).cuda()
        if loss_Dreal is None:
            loss_Dreal = torch.Tensor([0]).cuda()

        return loss_l1.mean(), loss_vgg.mean(), loss_Dreal.mean(), loss_Gmain.mean(), loss_Dgen.mean()
            
        

#----------------------------------------------------------------------------
