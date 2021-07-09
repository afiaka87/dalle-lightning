import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
import math
from einops import rearrange

from pl_dalle.modules.diffusionmodules.model import Encoder, Decoder, VUNet
from pl_dalle.modules.vqvae.quantize import VectorQuantizer
from pl_dalle.modules.vqvae.quantize import GumbelQuantize


class VQVAE(pl.LightningModule):
    def __init__(self,
                 args,batch_size, learning_rate,
                 ignore_keys=[],
                 monitor=None,
                 remap=None,
                 same_index_shape=False,  # tell vector quantizer to return indices as bhw
                 normalization = ((0.5,) * 3, (0.5,) * 3)
                 ):
        super().__init__()
        self.save_hyperparameters()
        self.args = args     
        
        self.encoder = Encoder(ch=args.ch, out_ch=args.out_ch, ch_mult= args.ch_mult,
                                num_res_blocks=args.num_res_blocks, 
                                attn_resolutions=args.attn_resolutions,
                                dropout=args.dropout, in_channels=args.in_channels, 
                                resolution=args.resolution, z_channels=args.z_channels,
                                double_z=args.double_z)

        self.decoder = Decoder(ch=args.ch, out_ch=args.out_ch, ch_mult= args.ch_mult,
                                num_res_blocks=args.num_res_blocks, 
                                attn_resolutions=args.attn_resolutions,
                                dropout=args.dropout, in_channels=args.in_channels, 
                                resolution=args.resolution, z_channels=args.z_channels)
        
        self.normalization = normalization
        self.smooth_l1_loss = args.smooth_l1_loss
        self.quantize = VectorQuantizer(args.n_embed, args.embed_dim, beta=0.25,
                                        remap=remap, same_index_shape=same_index_shape)
        self.quant_conv = torch.nn.Conv2d(args.z_channels, args.embed_dim, 1)
        self.post_quant_conv = torch.nn.Conv2d(args.embed_dim, args.z_channels, 1)


        if monitor is not None:
            self.monitor = monitor

    def norm(self, images):
        if not exists(self.normalization):
            return images

        means, stds = map(lambda t: torch.as_tensor(t).to(images), self.normalization)
        means, stds = map(lambda t: rearrange(t, 'c -> () c () ()'), (means, stds))
        images = images.clone()
        images.sub_(means).div_(stds)
        return images

    def encode(self, x):
        h = self.encoder(x)
        h = self.quant_conv(h)
        quant, emb_loss, info = self.quantize(h)
        return quant, emb_loss, info

    def decode(self, quant):
        quant = self.post_quant_conv(quant)
        dec = self.decoder(quant)
        return dec

    def decode_code(self, code_b):
        quant_b = self.quantize.embed_code(code_b)
        dec = self.decode(quant_b)
        return dec

    @torch.no_grad()
    def get_codebook_indices(self, img):
        b = img.shape[0]
        img = (2 * img) - 1
        _, _, [_, _, indices] = self.model.encode(img)
        return rearrange(indices, '(b n) -> b n', b = b)

    def forward(self, input):
        quant, diff, _ = self.encode(input)
        dec = self.decode(quant)
        return dec, diff

    def training_step(self, batch, batch_idx):     
        x, _ = batch
        xrec, qloss = self(x)
        if self.smooth_l1_loss:
            aeloss = F.smooth_l1_loss(x, xrec, self.global_step)
        else:
            aeloss = F.mse_loss(x, xrec, self.global_step)            
        self.log("train/rec_loss", aeloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        self.log("train/embed_loss", qloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return aeloss + qloss

    def validation_step(self, batch, batch_idx):
        x, _ = batch
        xrec, qloss = self(x)
        if self.smooth_l1_loss:
            aeloss = F.smooth_l1_loss(x, xrec, self.global_step)
        else:
            aeloss = F.mse_loss(x, xrec, self.global_step)            
        self.log("val/rec_loss", aeloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        self.log("val/embed_loss", qloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return aeloss + qloss


    def configure_optimizers(self):
        lr = self.hparams.learning_rate
        opt = torch.optim.Adam(self.parameters,lr=lr, betas=(0.5, 0.9))
        sched = torch.optim.lr_scheduler.ExponentialLR(optimizer = opt, gamma = self.args.lr_decay_rate)
        return opt, sched

    def get_last_layer(self):
        return self.decoder.conv_out.weight
        
    def log_images(self, batch, **kwargs):
        log = dict()
        x, _ = batch
        x = x.to(self.device)
        xrec, _ = self(x)
        if x.shape[1] > 3:
            # colorize with random projection
            assert xrec.shape[1] > 3
            x = self.to_rgb(x)
            xrec = self.to_rgb(xrec)
        log["inputs"] = x
        log["reconstructions"] = xrec
        return log


class GumbelVQVAE(VQVAE):
    def __init__(self,
                 args, batch_size, learning_rate,
                 ignore_keys=[],
                 monitor=None,
                 remap=None,
                 ):
        self.save_hyperparameters()
        self.args = args    
        super().__init__(args, batch_size, learning_rate,
                         ignore_keys=ignore_keys,
                         monitor=monitor,
                         )

        self.loss.n_classes = args.n_embed
        self.vocab_size = args.n_embed

        self.quantize = GumbelQuantize(args.z_channels, args.embed_dim,
                                       n_embed=args.n_embed,
                                       kl_weight=args.kl_loss_weight, temp_init=args.starting_temp,
                                       remap=remap)


    def encode_to_prequant(self, x):
        h = self.encoder(x)
        h = self.quant_conv(h)
        return h

    def decode_code(self, code_b):
        raise NotImplementedError

    @torch.no_grad()
    def get_codebook_indices(self, img):
        b = img.shape[0]
        img = (2 * img) - 1
        _, _, [_, _, indices] = self.model.encode(img)
        return rearrange(indices, 'b h w -> b (h w)', b=b)

    def training_step(self, batch, batch_idx, optimizer_idx):
        x, _ = batch
        self.temperature = max(self.temperature * math.exp(-self.anneal_rate * self.global_step), self.temp_min)
        self.quantize.temperature = self.temperature
        xrec, qloss = self(x)

        if self.smooth_l1_loss:
            aeloss = F.smooth_l1_loss(x, xrec, self.global_step)
        else:
            aeloss = F.mse_loss(x, xrec, self.global_step)            
        self.log("train/rec_loss", aeloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        self.log("train/embed_loss", qloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return aeloss + qloss

    def validation_step(self, batch, batch_idx):
        x, _ = batch
        xrec, qloss = self(x)
        self.quantize.temperature = 1.0
        if self.smooth_l1_loss:
            aeloss = F.smooth_l1_loss(x, xrec, self.global_step)
        else:
            aeloss = F.mse_loss(x, xrec, self.global_step)            
        self.log("val/rec_loss", aeloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        self.log("val/embed_loss", qloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return aeloss + qloss

    def log_images(self, batch, **kwargs):
        log = dict()
        x, _ = batch
        x = x.to(self.device)
        # encode
        h = self.encoder(x)
        h = self.quant_conv(h)
        quant, _, _ = self.quantize(h)
        # decode
        x_rec = self.decode(quant)
        log["inputs"] = x
        log["reconstructions"] = x_rec
        return log        