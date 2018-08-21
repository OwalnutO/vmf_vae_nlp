import torch
from scipy import special as sp
import numpy as np
from NVLL.util.util import GVar
from NVLL.util.gpu_flag import device


class BesselIve(torch.autograd.Function):
    """
    We can implement our own custom autograd Functions by subclassing
    torch.autograd.Function and implementing the forward and backward passes
    which operate on Tensors.
    """

    @staticmethod
    def forward(ctx, dim, kappa):
        """
        In the forward pass we receive a Tensor containing the input and return
        a Tensor containing the output. ctx is a context object that can be used
        to stash information for backward computation. You can cache arbitrary
        objects for use in the backward pass using the ctx.save_for_backward method.
        """
        ctx.save_for_backward(dim.float(), kappa)
        m = sp.ive(dim, kappa.detach())
        x = torch.tensor(m).to(device)
        # x = torch.from_numpy(np.asarray(sp.ive(dim, kappa)))
        return x

    @staticmethod
    def backward(ctx, grad_output):
        """
        In the backward pass we receive a Tensor containing the gradient of the loss
        with respect to the output, and we need to compute the gradient of the loss
        with respect to the input.
        """
        dim, kappa = ctx.saved_tensors
        grad_input = grad_output.clone()
        grad = grad_input * (bessel(dim - 1, kappa) - bessel(dim, kappa) * (dim + kappa) / kappa)
        return None, grad


bessel = BesselIve.apply


class VmfDiff(torch.nn.Module):
    def __init__(self, hid_dim, lat_dim):
        super().__init__()
        self.hid_dim = hid_dim
        self.lat_dim = lat_dim
        self.func_mu = torch.nn.Linear(hid_dim, lat_dim)
        self.func_kappa = torch.nn.Linear(hid_dim, 1)
        # self.kld = GVar(torch.from_numpy(vMF._vmf_kld(kappa, lat_dim)).float())
        # print('KLD: {}'.format(self.kld.data[0]))
        self.nonneg = torch.nn.Softplus()

    def estimate_param(self, latent_code):
        ret_dict = {}
        ret_dict['kappa'] = self.nonneg(self.func_kappa(latent_code))

        # Only compute mu, use mu/mu_norm as mu,
        #  use 1 as norm, use diff(mu_norm, 1) as redundant_norm
        mu = self.func_mu(latent_code)

        norm = torch.norm(mu, 2, 1, keepdim=True)
        mu_norm_sq_diff_from_one = torch.pow(torch.add(norm, -1), 2)
        redundant_norm = torch.sum(mu_norm_sq_diff_from_one, dim=1, keepdim=True)
        ret_dict['norm'] = torch.ones_like(mu)
        ret_dict['redundant_norm'] = redundant_norm

        mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
        ret_dict['mu'] = mu

        return ret_dict

    def compute_KLD(self, tup, batch_sz):
        kappa = tup['kappa']
        d = self.lat_dim

        rt_bag = []
        # const = torch.log(torch.tensor(3.1415926)) * d / 2 + torch.log(torch.tensor(2.0)) \
        #         - torch.tensor(sp.loggamma(d / 2).real) - (d / 2) * torch.log(torch.tensor(2 * 3.1415926))

        const = torch.tensor(
            np.log(3.1415926) * d / 2 + np.log(2) - sp.loggamma(d / 2).real - (d / 2) * np.log(2 * 3.1415926)).to(
            device)
        # print(const)
        d = torch.tensor([d], dtype=torch.float).to(device)
        batchsz = kappa.size()[0]
        for k_idx in range(batchsz):
            k = kappa[k_idx]
            # print(k)
            # print(k)
            # print(d)
            first = k * bessel(d / 2, k) / bessel(d / 2 - 1, k)
            second = (d / 2 - 1) * torch.log(k) - torch.log(bessel(d / 2 - 1, k))
            combin = (first + second + const)
            # print(combin)
            rt_bag.append(combin)
        return torch.tensor(rt_bag).to(device)

    def build_bow_rep(self, lat_code, n_sample):
        batch_sz = lat_code.size()[0]
        tup = self.estimate_param(latent_code=lat_code)
        mu = tup['mu']
        norm = tup['norm']
        kappa = tup['kappa']

        kld = self.compute_KLD(tup, batch_sz)
        vecs = []
        kappa = kappa.detach().cpu().numpy()
        if n_sample == 1:
            return tup, kld, self.sample_cell(mu, norm, kappa)
        for n in range(n_sample):
            sample = self.sample_cell(mu, norm, kappa)
            vecs.append(sample)
        vecs = torch.cat(vecs, dim=0)
        return tup, kld, vecs

    def sample_cell(self, mu, norm, kappa):
        batch_sz, lat_dim = mu.size()
        # mu = GVar(mu)
        mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
        w = self._sample_weight_batch(kappa, lat_dim, batch_sz)
        w = w.unsqueeze(1)

        # batch version
        w_var = GVar(w * torch.ones(batch_sz, lat_dim).to(device))
        v = self._sample_ortho_batch(mu, lat_dim)
        scale_factr = torch.sqrt(
            GVar(torch.ones(batch_sz, lat_dim)) - torch.pow(w_var, 2))
        orth_term = v * scale_factr
        muscale = mu * w_var
        sampled_vec = orth_term + muscale

        return sampled_vec.unsqueeze(0).to(device)

    def _sample_weight_batch(self, kappa, dim, batch_sz=1):
        # result = torch.FloatTensor((batch_sz))
        result = np.zeros((batch_sz))
        for b in range(batch_sz):
            result[b] = self._sample_weight(kappa[b], dim)
        return torch.from_numpy(result).float().to(device)

    def _sample_weight(self, kappa, dim):
        """Rejection sampling scheme for sampling distance from center on
        surface of the sphere.
        """
        dim = dim - 1  # since S^{n-1}
        # print(dim)
        # print(kappa)
        b = dim / (np.sqrt(4. * kappa ** 2 + dim ** 2) + 2 * kappa)  # b= 1/(sqrt(4.* kdiv**2 + 1) + 2 * kdiv)
        x = (1. - b) / (1. + b)
        c = kappa * x + dim * np.log(1 - x ** 2)  # dim * (kdiv *x + np.log(1-x**2))

        while True:
            z = np.random.beta(dim / 2., dim / 2.)  # concentrates towards 0.5 as d-> inf
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = np.random.uniform(low=0, high=1)
            if kappa * w + dim * np.log(1. - x * w) - c >= np.log(
                    u):  # thresh is dim *(kdiv * (w-x) + log(1-x*w) -log(1-x**2))
                return w

    def _sample_ortho_batch(self, mu, dim):
        """

        :param mu: Variable, [batch size, latent dim]
        :param dim: scala. =latent dim
        :return:
        """
        _batch_sz, _lat_dim = mu.size()
        assert _lat_dim == dim
        squeezed_mu = mu.unsqueeze(1)

        v = GVar(torch.randn(_batch_sz, dim, 1))  # TODO random

        # v = GVar(torch.linspace(-1, 1, steps=dim))
        # v = v.expand(_batch_sz, dim).unsqueeze(2)

        rescale_val = torch.bmm(squeezed_mu, v).squeeze(2)
        proj_mu_v = mu * rescale_val
        ortho = v.squeeze() - proj_mu_v
        ortho_norm = torch.norm(ortho, p=2, dim=1, keepdim=True)
        y = ortho / ortho_norm
        return y

    def _sample_orthonormal_to(self, mu, dim):
        """Sample point on sphere orthogonal to mu.
        """
        v = GVar(torch.randn(dim))  # TODO random

        # v = GVar(torch.linspace(-1,1,steps=dim))

        rescale_value = mu.dot(v) / mu.norm()
        proj_mu_v = mu * rescale_value.expand(dim)
        ortho = v - proj_mu_v
        ortho_norm = torch.norm(ortho)
        return ortho / ortho_norm.expand_as(ortho)

#
# a = torch.tensor(10)
# b = torch.ones(1, dtype=torch.float, requires_grad=True)
#
# y = bessel(a, b)
# loss = 1 - y
# print(y)
# loss.backward()
# print(a)