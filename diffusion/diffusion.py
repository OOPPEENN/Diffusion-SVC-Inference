from collections import deque
from functools import partial
from inspect import isfunction
import torch.nn.functional as F
import numpy as np
import torch
from torch import nn
import tqdm

def exists(x):
    return x is not None

def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d

def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

def noise_like(shape, device, repeat=False):
    repeat_noise = lambda: torch.randn((1, *shape[1:]), device=device).repeat(shape[0], *((1,) * (len(shape) - 1)))
    noise = lambda: torch.randn(shape, device=device)
    return repeat_noise() if repeat else noise()

def linear_beta_schedule(timesteps, max_beta=0.02):
    betas = np.linspace(1e-4, max_beta, timesteps)
    return betas

def cosine_beta_schedule(timesteps, s=0.008):
    steps = timesteps + 1
    x = np.linspace(0, steps, steps)
    alphas_cumprod = np.cos(((x / steps) + s) / (1 + s) * np.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return np.clip(betas, a_min=0, a_max=0.999)

beta_schedule = {
    "cosine": cosine_beta_schedule,
    "linear": linear_beta_schedule,
}


class GaussianDiffusion(nn.Module):
    def __init__(self, denoise_fn, out_dims=128, timesteps=1000, k_step=1000, max_beta=0.02, spec_min=-12, spec_max=2):
        super().__init__()
        self.denoise_fn = denoise_fn
        self.out_dims = out_dims
        betas = beta_schedule['linear'](timesteps, max_beta=max_beta)

        alphas = 1. - betas
        alphas_cumprod = np.cumprod(alphas, axis=0)
        alphas_cumprod_prev = np.append(1., alphas_cumprod[:-1])

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)
        self.k_step = k_step
        self.noise_list = deque(maxlen=4)

        to_torch = partial(torch.tensor, dtype=torch.float32)

        self.register_buffer('betas', to_torch(betas))
        self.register_buffer('alphas_cumprod', to_torch(alphas_cumprod))
        self.register_buffer('alphas_cumprod_prev', to_torch(alphas_cumprod_prev))
        self.register_buffer('sqrt_alphas_cumprod', to_torch(np.sqrt(alphas_cumprod)))
        self.register_buffer('sqrt_one_minus_alphas_cumprod', to_torch(np.sqrt(1. - alphas_cumprod)))
        self.register_buffer('log_one_minus_alphas_cumprod', to_torch(np.log(1. - alphas_cumprod)))
        self.register_buffer('sqrt_recip_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod)))
        self.register_buffer('sqrt_recipm1_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod - 1)))

        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod)
        self.register_buffer('posterior_variance', to_torch(posterior_variance))
        self.register_buffer('posterior_log_variance_clipped', to_torch(np.log(np.maximum(posterior_variance, 1e-20))))
        self.register_buffer('posterior_mean_coef1', to_torch(betas * np.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod)))
        self.register_buffer('posterior_mean_coef2', to_torch((1. - alphas_cumprod_prev) * np.sqrt(alphas) / (1. - alphas_cumprod)))
        self.register_buffer('spec_min', torch.FloatTensor([spec_min])[None, None, :out_dims])
        self.register_buffer('spec_max', torch.FloatTensor([spec_max])[None, None, :out_dims])

    def q_mean_variance(self, x_start, t):
        mean = extract(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
        variance = extract(1. - self.alphas_cumprod, t, x_start.shape)
        log_variance = extract(self.log_one_minus_alphas_cumprod, t, x_start.shape)
        return mean, variance, log_variance

    def predict_start_from_noise(self, x_t, t, noise):
        return (extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * noise)

    def q_posterior(self, x_start, x_t, t):
        posterior_mean = (extract(self.posterior_mean_coef1, t, x_t.shape) * x_start + extract(self.posterior_mean_coef2, t, x_t.shape) * x_t)
        posterior_variance = extract(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def p_mean_variance(self, x, t, cond, reference_mel=None):
        denoise_input = torch.cat([x[:,0,:,:], cond], dim=-2)
        noise_pred = self.denoise_fn(denoise_input, t, reference_mel).sample[:,None,:,:]
        x_recon = self.predict_start_from_noise(x, t=t, noise=noise_pred)
        x_recon.clamp_(-1., 1.)
        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start=x_recon, x_t=x, t=t)
        return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(self, x, t, cond, reference_mel=None, repeat_noise=False):
        b, *_, device = *x.shape, x.device
        model_mean, _, model_log_variance = self.p_mean_variance(x=x, t=t, cond=cond,reference_mel=reference_mel)
        noise = noise_like(x.shape, device, repeat_noise)
        nonzero_mask = (1 - (t == 0).float()).reshape(b, *((1,) * (len(x.shape) - 1)))
        return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise

    @torch.no_grad()
    def p_sample_ddim(self, x, t, interval, cond, reference_mel=None):
        a_t = extract(self.alphas_cumprod, t, x.shape)
        a_prev = extract(self.alphas_cumprod, torch.max(t - interval, torch.zeros_like(t)), x.shape)
        denoise_input = torch.cat([x[:,0,:,:], cond], dim=-2)
        noise_pred = self.denoise_fn(denoise_input, t, reference_mel).sample[:,None,:,:]
        x_prev = a_prev.sqrt() * (x / a_t.sqrt() + (((1 - a_prev) / a_prev).sqrt()-((1 - a_t) / a_t).sqrt()) * noise_pred)
        return x_prev

    @torch.no_grad()
    def p_sample_plms(self, x, t, interval, cond, reference_mel=None):

        def get_x_pred(x, noise_t, t):
            a_t = extract(self.alphas_cumprod, t, x.shape)
            a_prev = extract(self.alphas_cumprod, torch.max(t - interval, torch.zeros_like(t)), x.shape)
            a_t_sq, a_prev_sq = a_t.sqrt(), a_prev.sqrt()
            x_delta = (a_prev - a_t) * ((1 / (a_t_sq * (a_t_sq + a_prev_sq))) * x - 1 / (a_t_sq * (((1 - a_prev) * a_t).sqrt() + ((1 - a_t) * a_prev).sqrt())) * noise_t)
            x_pred = x + x_delta
            return x_pred

        denoise_input = torch.cat([x[:,0,:,:], cond], dim=-2)
        noise_list = self.noise_list
        noise_pred = self.denoise_fn(denoise_input, t, reference_mel).sample[:,None,:,:]

        if len(noise_list) == 0:
            x_pred = get_x_pred(x, noise_pred, t)
            denoise_input = torch.cat([x_pred[:,0,:,:], cond], dim=-2)
            noise_pred_prev = self.denoise_fn(denoise_input, max(t - interval, 0), reference_mel).sample[:,None,:,:]
            noise_pred_prime = (noise_pred + noise_pred_prev) / 2
        elif len(noise_list) == 1:
            noise_pred_prime = (3 * noise_pred - noise_list[-1]) / 2
        elif len(noise_list) == 2:
            noise_pred_prime = (23 * noise_pred - 16 * noise_list[-1] + 5 * noise_list[-2]) / 12
        else:
            noise_pred_prime = (55 * noise_pred - 59 * noise_list[-1] + 37 * noise_list[-2] - 9 * noise_list[-3]) / 24

        x_prev = get_x_pred(x, noise_pred_prime, t)
        noise_list.append(noise_pred)

        return x_prev

    def q_sample(self, x_start, t, noise=None):
        noise = default(noise, lambda: torch.randn_like(x_start))
        return (extract(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start + extract(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * noise)

    def p_losses(self, x_start, t, cond, reference_mel=None, noise=None, loss_type='l2'):
        noise = default(noise, lambda: torch.randn_like(x_start))

        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        denoise_input = torch.cat([x_noisy[:,0,:,:], cond], dim=-2)
        x_recon = self.denoise_fn(denoise_input, t, reference_mel).sample[:,None,:,:]

        if loss_type == 'l1':
            loss = (noise - x_recon).abs().mean()
        elif loss_type == 'l2':
            loss = F.mse_loss(noise, x_recon)
        else:
            raise NotImplementedError()

        return loss

    def forward(self, condition, reference_mel=None, gt_spec=None, infer=True, infer_speedup=10, method='dpm-solver', k_step=None, use_tqdm=True):
        cond = condition.transpose(1, 2)
        b, device = condition.shape[0], condition.device

        if not infer:
            spec = self.norm_spec(gt_spec)
            if k_step is None:
                t_max = self.k_step
            else:
                t_max = k_step
            t = torch.randint(0, t_max, (b,), device=device).long()
            norm_spec = spec.transpose(1, 2)[:, None, :, :]
            return self.p_losses(norm_spec, t, cond=cond,reference_mel=reference_mel)
        else:
            shape = (cond.shape[0], 1, self.out_dims, cond.shape[2])

            if gt_spec is None or k_step is None:
                t = self.k_step
                x = torch.randn(shape, device=device)
            else:
                t = k_step
                norm_spec = self.norm_spec(gt_spec)
                norm_spec = norm_spec.transpose(1, 2)[:, None, :, :]
                x = self.q_sample(x_start=norm_spec, t=torch.tensor([t - 1], device=device).long())

            if method is not None and infer_speedup > 1:

                if method == 'dpm-solver':
                    from .dpm_solver_pytorch import NoiseScheduleVP, model_wrapper, DPM_Solver
                    noise_schedule = NoiseScheduleVP(schedule='discrete', betas=self.betas[:t])
                    def my_wrapper(fn):
                        def wrapped(x, t, cond, **kwargs):
                            denoise_input = torch.cat([x[:,0,:,:], cond], dim=-2)
                            ret = fn(denoise_input, t, **kwargs).sample[:,None,:,:]
                            if use_tqdm:
                                self.bar.update(1)
                            return ret
                        return wrapped

                    model_fn = model_wrapper(my_wrapper(self.denoise_fn), noise_schedule, model_type="noise", model_kwargs={"cond": cond,"encoder_hidden_states":reference_mel})
                    dpm_solver = DPM_Solver(model_fn, noise_schedule, algorithm_type="dpmsolver++")

                    steps = t // infer_speedup
                    if use_tqdm:
                        self.bar = tqdm.tqdm(total=steps, desc='Sample Steps')

                    x = dpm_solver.sample(x, steps=steps, order=2, skip_type="time_uniform", method="multistep")
                    if use_tqdm:
                        self.bar.close()

                elif method == 'unipc':
                    from .uni_pc import NoiseScheduleVP, model_wrapper, UniPC
                    noise_schedule = NoiseScheduleVP(schedule='discrete', betas=self.betas[:t])

                    def my_wrapper(fn):
                        def wrapped(x, t, cond, **kwargs):
                            denoise_input = torch.cat([x[:,0,:,:], cond], dim=-2)
                            ret = fn(denoise_input, t, **kwargs).sample[:,None,:,:]
                            if use_tqdm:
                                self.bar.update(1)
                            return ret
                        return wrapped

                    model_fn = model_wrapper(my_wrapper(self.denoise_fn), noise_schedule, model_type="noise", model_kwargs={"cond": cond,"encoder_hidden_states":reference_mel})
                    uni_pc = UniPC(model_fn, noise_schedule, variant='bh2')

                    steps = t // infer_speedup
                    if use_tqdm:
                        self.bar = tqdm.tqdm(total=steps, desc='Sample Steps')

                    x = uni_pc.sample(x, steps=steps, order=2, skip_type="time_uniform", method="multistep")
                    if use_tqdm:
                        self.bar.close()

                elif method == 'pndm':
                    self.noise_list = deque(maxlen=4)
                    if use_tqdm:
                        for i in tqdm.tqdm(reversed(range(0, t, infer_speedup)), total=t // infer_speedup, desc='Sample Steps'):
                            x = self.p_sample_plms(x, torch.full((b,), i, device=device, dtype=torch.long), infer_speedup, cond=cond, reference_mel=reference_mel)
                    else:
                        for i in reversed(range(0, t, infer_speedup)):
                            x = self.p_sample_plms(x, torch.full((b,), i, device=device, dtype=torch.long), infer_speedup, cond=cond, reference_mel=reference_mel)

                elif method == 'ddim':
                    if use_tqdm:
                        for i in tqdm.tqdm(reversed(range(0, t, infer_speedup)), total=t // infer_speedup, desc='Sample Steps'):
                            x = self.p_sample_ddim(x, torch.full((b,), i, device=device, dtype=torch.long), infer_speedup, cond=cond, reference_mel=reference_mel)
                    else:
                        for i in reversed(range(0, t, infer_speedup)):
                            x = self.p_sample_ddim(x, torch.full((b,), i, device=device, dtype=torch.long), infer_speedup, cond=cond, reference_mel=reference_mel)
                else:
                    raise NotImplementedError(method)
            else:
                if use_tqdm:
                    for i in tqdm(reversed(range(0, t)), total=t):
                        x = self.p_sample(x, torch.full((b,), i, device=device, dtype=torch.long), cond, reference_mel=reference_mel)
                else:
                    for i in reversed(range(0, t)):
                        x = self.p_sample(x, torch.full((b,), i, device=device, dtype=torch.long), cond, reference_mel=reference_mel)
            x = x.squeeze(1).transpose(1, 2)  # [B, T, M]
            return self.denorm_spec(x)

    def norm_spec(self, x):
        return (x - self.spec_min) / (self.spec_max - self.spec_min) * 2 - 1

    def denorm_spec(self, x):
        return (x + 1) / 2 * (self.spec_max - self.spec_min) + self.spec_min
