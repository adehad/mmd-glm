import numpy as np
import torch
from torch.nn import Parameter
from torch.optim import Adam, LBFGS

from sptr.sptr import SpikeTrain

from .base import GLM
from ..metrics import _mmd_from_features, _mmd_from_gramians
from ..utils import get_dt, shift_array

dic_nonlinearities = {'exp': lambda x: torch.exp(x), 'log_exp': lambda x: torch.log(1 + torch.exp(x))}

class MMDGLM2(GLM, torch.nn.Module):

    def __init__(self, u0=0, kappa=None, eta=None, non_linearity='exp'):
        torch.nn.Module.__init__(self)
        GLM.__init__(self, u0=u0, kappa=kappa, eta=eta, non_linearity=non_linearity)
        self.non_linearity_torch = dic_nonlinearities[non_linearity]
        
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis

        b = torch.tensor([u0]).double()
        self.register_parameter("b", torch.nn.Parameter(b))
        
        if self.kappa is not None:
            kappa_coefs = torch.from_numpy(kappa.coefs)
            self.register_parameter("kappa_coefs", torch.nn.Parameter(kappa_coefs))
        if self.eta is not None:
            eta_coefs = torch.from_numpy(eta.coefs)
            self.register_parameter("eta_coefs", torch.nn.Parameter(eta_coefs))
    
    def forward(self, t, stim=None, n_batch_fr=None):
        
        dt = get_dt(t)
        theta_g = self.get_params()
        
        # TODO. I am calculating u_fr and r_fr twice because I can't backpropagate through my current sample function. change this
        if stim is not None:
            _, _, mask_spikes_fr = self.sample(t, stim=stim)
        else:
            _, _, mask_spikes_fr = self.sample(t, shape=(n_batch_fr,))

        X_fr = torch.from_numpy(self.objective_kwargs(t, mask_spikes_fr, stim=stim)['X'])
        u_fr = torch.einsum('tka,a->tk', X_fr, theta_g)
        r_fr = self.non_linearity_torch(u_fr)
        mask_spikes_fr = torch.from_numpy(mask_spikes_fr)
        
        return r_fr, mask_spikes_fr, X_fr
    
    def get_params(self):
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis
        theta = torch.zeros(1 + n_kappa + n_eta)
        theta[0] = self.b
        if self.kappa is not None:
            theta[1:1 + n_kappa] = self.kappa_coefs
        if self.eta is not None:
            theta[1 + n_kappa:] = self.eta_coefs
        theta = theta.double()
        return theta
    
    def _neg_log_likelihood(self, dt, mask_spikes, X_dc):
        theta_g = self.get_params()
        u_dc = torch.einsum('tka,a->tk', X_dc, theta_g)
        r_dc = self.non_linearity_torch(u_dc)
        neg_log_likelihood = -(torch.sum(torch.log(1 - torch.exp(-dt * r_dc)) * mask_spikes.double()) - \
                               dt * torch.sum(r_dc * (1 - mask_spikes.double())))
        return neg_log_likelihood
    
    def _score(self, dt, mask_spikes, X):
        with torch.no_grad():
            theta_g = self.get_params().detach()
            u = torch.einsum('tka,a->tk', X, theta_g)
            r = self.non_linearity_torch(u)
            exp_r = torch.exp(r * dt)
            score = dt * torch.einsum('tka,tk->ka', X, r / (exp_r - 1) * mask_spikes.double()) - \
                    dt * torch.einsum('tka,tk->ka', X, r * (1 - mask_spikes.double()))
        return score
    
    def train(self, t, mask_spikes, phi=None, kernel=None, stim=None, log_likelihood=False, lam_mmd=1e0, biased=False, 
              biased_mmd=False, optim=None, scheduler=None, num_epochs=20, n_batch_fr=100, kernel_kwargs=None, control_variates=False, clip=None, verbose=False, 
              mmd_kwargs=None, metrics=None, n_metrics=25, n_iterations_store=1, beta=1):

        n_d = mask_spikes.shape[1]
        dt = torch.tensor([get_dt(t)])
        loss, nll, mmd = [], [], []
        
        X_dc = torch.from_numpy(self.objective_kwargs(t, mask_spikes, stim=stim)['X']).double()
        
        kernel_kwargs = kernel_kwargs if kernel_kwargs is not None else {}
        
        if phi is not None:
            phi_d = phi(t, mask_spikes, **kernel_kwargs)
            sum_phi_d = torch.sum(phi_d, 1)
        else:
            idx_fr = np.triu_indices(n_batch_fr, k=1)
            idx_fr = (torch.from_numpy(idx_fr[0]), torch.from_numpy(idx_fr[1]))
            idx_d = np.triu_indices(n_d, k=1)
            idx_d = (torch.from_numpy(idx_d[0]), torch.from_numpy(idx_d[1]))
            gramian_d_d = kernel(t, mask_spikes, mask_spikes, **kernel_kwargs)
            
        _loss = torch.tensor([np.nan])

        r_fr_list, mask_spikes_fr_list = [], []
        for epoch in range(num_epochs):
            if verbose:
                print('\r', 'epoch', epoch, 'of', num_epochs, 
                      'loss', np.round(_loss.item(), 10), end='')
            
            optim.zero_grad()
            
            _r_fr, _mask_spikes_fr, X_fr = self(t, stim=stim, n_batch_fr=n_batch_fr)
            
            if len(mask_spikes_fr_list) >= n_iterations_store:
                r_fr_list.pop(0)
                mask_spikes_fr_list.pop(0)
                
#             print(len(mask_spikes_fr_list))
            
            r_fr_list.append(_r_fr)
            mask_spikes_fr_list.append(_mask_spikes_fr)
            r_fr = torch.cat(r_fr_list, 1)
            mask_spikes_fr = torch.cat(mask_spikes_fr_list, 1)
            
            log_proba = torch.sum(torch.log(1 - torch.exp(-dt * r_fr) + 1e-24) * mask_spikes_fr.double(), 0) - \
                        dt * torch.sum(r_fr * (1 - mask_spikes_fr.double()), 0)
            
            if phi is not None:
                phi_fr = phi(t, mask_spikes_fr, **kernel_kwargs)
                weights = np.repeat(beta**np.arange(len(mask_spikes_fr_list)), n_batch_fr)[None, :]
                phi_fr = phi_fr * weights
                
                if not biased:
                    log_proba_phi = log_proba[None, :] * phi_fr
                    sum_log_proba_phi_fr = torch.sum(log_proba_phi, 1)
                    sum_phi_fr = torch.sum(phi_fr, 1)
                    norm2_fr = (torch.sum(sum_log_proba_phi_fr * sum_phi_fr) - torch.sum(log_proba_phi * phi_fr)) / (n_batch_fr * (n_batch_fr - 1))
                    mmd_surr = 2 * norm2_fr - 2 / (n_d * n_batch_fr) * torch.sum(sum_phi_d * sum_log_proba_phi_fr)
                else:
                    mmd_surr = -2 * torch.sum((torch.mean(phi_d, 1) - torch.mean(phi_fr, 1)) * torch.mean(log_proba[None, :] * phi_fr, 1)) # esto esta bien?
#                     mmd_surr /= torch.sum((torch.mean(phi_d, 1) - torch.mean(phi_fr, 1))**2)**0.5
#                     mmd_surr = 0.5 * torch.sum((torch.mean(phi_d, 1) - torch.mean(phi_fr, 1))**2)**(-1/2) * \
#                                                (-2) * torch.sum((torch.mean(phi_d, 1) - torch.mean(phi_fr, 1)) * torch.mean(log_proba[None, :] * phi_fr, 1))
            else:
                gramian_fr_fr = kernel(t, mask_spikes_fr, mask_spikes_fr, **kernel_kwargs)
                gramian_d_fr = kernel(t, mask_spikes, mask_spikes_fr, **kernel_kwargs)
                if not biased:
#                     mmd_surr = torch.mean(((log_proba[:, None] + log_proba[None, :]) * gramian_fr_fr)[idx_fr]) \
#                                               -2 * torch.mean(log_proba[None, :] * gramian_d_fr)
                    gramian_fr_fr.fill_diagonal_(0)
                    mmd_surr = 2 * torch.sum(log_proba[:, None] * gramian_fr_fr) / (n_batch_fr * (n_batch_fr - 1)) \
                             - 2 * torch.mean(log_proba[None, :] * gramian_d_fr)
                else:
                    mmd_surr = torch.mean(((log_proba[:, None] + log_proba[None, :]) * gramian_fr_fr)) \
                                                  -2 * torch.mean(log_proba[None, :] * gramian_d_fr)
            
            _loss = lam_mmd * mmd_surr
            
            if log_likelihood:
                _nll = self._neg_log_likelihood(dt, mask_spikes, X_dc)
                _loss = _loss + _nll
                nll.append(_nll.item())
                        
            if not control_variates and not(n_iterations_store > 1):
                _loss.backward()
            else:
                _loss.backward(retain_graph=True)
            
            if control_variates:
                scores = self._score(dt, mask_spikes_fr, X_fr)
                mean_score = torch.mean(scores, dim=0)
                var_score = torch.var(scores, dim=0)
                
                if kernel is None:
                    gramian_fr_fr = torch.sum(phi_fr[:, :, None] * phi_fr[:, None, :], dim=0)
                    gramian_d_fr = torch.sum(phi_d[:, :, None] * phi_fr[:, None, :], dim=0)
                    
                mmd_weights = 2 * torch.sum(gramian_fr_fr, dim=1)[:, None] / (n_batch_fr * (n_batch_fr - 1)) - 2 * torch.sum(gramian_d_fr, dim=0)[:, None] / (n_batch_fr * n_d)
                corr_score = torch.mean(scores * scores * mmd_weights, dim=0)
                cov_score = corr_score - mean_score * torch.mean(scores * mmd_weights, dim=0)
                a = cov_score / var_score
                cum = 0
                for param in self.parameters():
                    n = param.shape[0]
                    param.backward(a[cum:cum + n] * mean_score[cum:cum + n])
                    cum += n
            if clip is not None:
                torch.nn.utils.clip_grad_value_(self.parameters(), clip)
            
            if epoch % n_metrics == 0:
                _metrics = metrics(self, t, mask_spikes, mask_spikes_fr) if metrics is not None else {}
                
                if kernel is not None:
                    _metrics['mmd'] = _mmd_from_gramians(t, gramian_d_d, gramian_fr_fr, gramian_d_fr, biased=biased).item()
                else:
                    _phi_fr = phi_fr[:, -n_batch_fr:]
                    _metrics['mmd'] = _mmd_from_features(t, phi_d, _phi_fr, biased=biased).item()
                    
                
#                 with torch.no_grad():
#                     logp = log_proba.numpy()
#                     scores = self._score(dt, mask_spikes_fr, X_fr).numpy()
#                     _metrics['log_proba'] = np.array([np.mean(logp), np.min(logp), np.median(logp), np.max(logp)])
#                     _metrics['scores'] = np.array([np.mean(scores, 0), np.min(scores, 0), np.median(scores, 0), np.max(scores, 0)])
#                     _metrics['scores'] = np.array([np.mean(scores, 0), np.min(scores, 0), np.median(scores, 0), np.max(scores, 0)])
#                     _metrics['norm2_fr'] = torch.sum(gramian_fr_fr) / (n_batch_fr * (n_batch_fr - 1))
#                     _metrics['mean_dot'] = torch.mean(gramian_d_fr)
#                     _metrics['norm2_fr'] = torch.mean(phi_fr**2)
#                     _metrics['mean_dot'] = torch.mean(gramian_d_fr)
                    
#                 if phi is not None:
#                     if not biased:
#                         _metrics['mmd'] = (torch.sum((torch.mean(phi_d.detach(), 1) - torch.mean(phi_fr.detach(), 1))**2)).item()
#                     else:
#                         sum_phi_d = torch.sum(phi_d, 1)
#                         sum_phi_fr = torch.sum(phi_fr, 1)
#                         norm2_d = (torch.sum(sum_phi_d**2) - torch.sum(phi_d**2)) / (n_d * (n_d - 1))
#                         norm2_fr = (torch.sum(sum_phi_fr**2) - torch.sum(phi_fr**2)) / (n_batch_fr * (n_batch_fr - 1))
#                         mean_dot = torch.sum(sum_phi_d * sum_phi_fr) / (n_d * n_batch_fr)
#                         _metrics['mmd'] = norm2_d + norm2_fr - 2 * mean_dot
#                 else:
#                     if not biased:
#                         _metrics['mmd'] = torch.mean(gramian_d_d.detach()[idx_d]) + torch.mean(gramian_fr_fr.detach()[idx_fr]) \
#                                           - 2 * torch.mean(gramian_d_fr.detach())
#                     else:
#                         _metrics['mmd'] = torch.mean(gramian_d_d.detach()) + torch.mean(gramian_fr_fr.detach()) \
#                                           - 2 * torch.mean(gramian_d_fr.detach())

                if epoch == 0:
                    metrics_list = {key:[val] for key, val in _metrics.items()}
                else:
                    for key, val in _metrics.items():
                        metrics_list[key].append(val)
                        
            optim.step()
            if scheduler is not None:
                scheduler.step()
            
            theta_g = self.get_params()
            self.set_params(theta_g.data.detach().numpy())
            
            loss.append(_loss.item())
            
#         if metrics is None:
#             metrics_list = None
        
        return loss, nll, metrics_list
