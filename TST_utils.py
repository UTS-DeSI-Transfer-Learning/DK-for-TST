
import numpy as np
import matplotlib.pyplot as plt
import torch
import torchvision
import torch.nn.functional as F
from past.utils import old_div
import pickle
import freqopttest.util as util
import freqopttest.data as data
import freqopttest.kernel as kernel
import freqopttest.tst as tst
import freqopttest.glo as glo

is_cuda = True

def get_item(x, is_cuda):
    if is_cuda:
        x = x.cpu().detach().numpy()
    else:
        x = x.detach().numpy()
    return x

def MatConvert(x, device, dtype):
    x = torch.from_numpy(x).to(device, dtype)
    return x


def Pdist2(x, y):
    x_norm = (x ** 2).sum(1).view(-1, 1)
    if y is not None:
        y_norm = (y ** 2).sum(1).view(1, -1)
    else:
        y = x
        y_norm = x_norm.view(1, -1)

    Pdist = x_norm + y_norm - 2.0 * torch.mm(x, torch.transpose(y, 0, 1))
    # Pdist = Pdist - torch.diag(Pdist.diag())
    Pdist[Pdist<0]=0
    return Pdist


def guassian_kernel(Fea, len_s, is_median = False):  # , kernel_mul=2.0, kernel_num=5, fix_sigma=None
    #    FeaALL = torch.cat([FeaS,FeaT],0)
    L2_distance = Pdist2(Fea, Fea)
    if is_median:
        L2D = L2_distance[0:len_s-1,len_s:Fea.size(0)]
        diss = L2D[L2D != 0]
        bandwidth = diss.median()
        kernel_val = torch.exp(-L2_distance /bandwidth)
    else:
        kernel_val = torch.exp(-L2_distance / 0.1)
    return kernel_val


def MyMMD(Fea, LM, len_s, is_median = False):  # , kernel_mul=2.0, kernel_num=5, fix_sigma=None
    kernels = guassian_kernel(Fea, len_s, is_median)
    loss = 0
    loss = kernels.mm(LM).trace()
    return loss


def h1_mean_var_gram(Kx, Ky, Kxy, is_var_computed, use_1sample_U=True):
    """
    Same as h1_mean_var() but takes in Gram matrices directly.
    """

    Kxxy = torch.cat((Kx,Kxy),1)
    Kyxy = torch.cat((Kxy.transpose(0,1),Ky),1)
    Kxyxy = torch.cat((Kxxy,Kyxy),0)

    nx = Kx.shape[0]
    ny = Ky.shape[0]
    is_unbiased = True
    if is_unbiased:
        xx = torch.div((torch.sum(Kx) - torch.sum(torch.diag(Kx))), (nx * (nx - 1)))
        yy = torch.div((torch.sum(Ky) - torch.sum(torch.diag(Ky))), (ny * (ny - 1)))

        # one-sample U-statistic.
        if use_1sample_U:
            xy = torch.div((torch.sum(Kxy) - torch.sum(torch.diag(Kxy))), (nx * (ny - 1)))
        else:
            xy = torch.div(torch.sum(Kxy), (nx * ny))
        mmd2 = xx - 2 * xy + yy
    else:
        xx = torch.div((torch.sum(Kx)), (nx * nx))
        yy = torch.div((torch.sum(Ky)), (ny * ny))

        # one-sample U-statistic.
        if use_1sample_U:
            xy = torch.div((torch.sum(Kxy)), (nx * ny))
        else:
            xy = torch.div(torch.sum(Kxy), (nx * ny))
        mmd2 = xx - 2 * xy + yy

    if not is_var_computed:
        return mmd2, None

    hh = Kx+Ky-Kxy-Kxy.transpose(0,1)
    V1 = torch.dot(hh.sum(1)/ny,hh.sum(1)/ny) / ny
    # V2 = (hh - torch.diag(torch.diag(hh))).sum() / (nx-1) / nx
    V2 = (hh).sum() / (nx) / nx
    varEst = 4*(V1 - V2**2)
    if  varEst == 0.0:
        print('error!!')
    ## unbiased var
    # hh = hh - torch.diag(torch.diag(hh))
    # # V1_t = torch.einsum('ij,in->ijn',[hh,hh])
    # # V1_diags = torch.einsum('...ii->...i', V1_t)
    # V1 = (torch.dot(hh.sum(1),hh.sum(1)) - (hh**2).sum())*6.0/ny/(ny-1)/(ny-2)/2.0
    # # V2_t = torch.einsum('ij,mn->ijmn', [hh, hh])
    # # V2_diags = torch.einsum('ijij->ij', V2_t)
    # V2 = (hh.sum()*hh.sum() - (hh**2).sum()) * 24.0 / ny / (ny - 1) / (ny - 2) / (ny - 3) /2.0
    # print(V1,V2)
    # varEst = 4 * (V1 - V2)
    return mmd2, varEst, Kxyxy


def MMDu(Fea, len_s, Fea_org, sigma, sigma0=0.1, is_smooth=True, is_mixture=False, beta=None, is_var_computed=True, use_1sample_U=True):
    """
    X: nxd numpy array
    Y: nxd numpy array
    k: a Kernel object
    is_var_computed: if True, compute the variance. If False, return None.
    use_1sample_U: if True, use one-sample U statistic for the cross term
      i.e., k(X, Y).

    Code based on Arthur Gretton's Matlab implementation for
    Bounliphone et. al., 2016.

    return (MMD^2, var[MMD^2]) under H1
    """
    X = Fea[0:len_s, :]
    Y = Fea[len_s:, :]
    X_org = Fea_org[0:len_s, :]
    Y_org = Fea_org[len_s:, :]
    epsilon = 10^(-10)

    nx = X.shape[0]
    ny = Y.shape[0]
    Dxx = Pdist2(X, X)
    Dyy = Pdist2(Y, Y)
    Dxy = Pdist2(X, Y)
    Dxx_org = Pdist2(X_org, X_org)
    Dyy_org = Pdist2(Y_org, Y_org)
    Dxy_org = Pdist2(X_org, Y_org)
    if is_mixture:
        if is_smooth:
            Kx = torch.exp(-Dxx / sigma0 - Dxx_org / sigma)
            Ky = torch.exp(-Dyy / sigma0 - Dyy_org / sigma)
            Kxy = torch.exp(-Dxy / sigma0 - Dxy_org / sigma)
        else:
            Kx = torch.exp(-Dxx / sigma0)
            Ky = torch.exp(-Dyy / sigma0)
            Kxy = torch.exp(-Dxy / sigma0)
    else:
        if is_smooth:
            Kx = (1-epsilon) * torch.exp(-Dxx / sigma0 - Dxx_org / sigma) + epsilon * torch.exp(-Dxx_org / sigma)
            Ky = (1-epsilon) * torch.exp(-Dyy / sigma0 - Dyy_org / sigma) + epsilon * torch.exp(-Dyy_org / sigma)
            Kxy = (1-epsilon) * torch.exp(-Dxy / sigma0 - Dxy_org / sigma) + epsilon * torch.exp(-Dxy_org / sigma)
        else:
            Kx = torch.exp(-Dxx / sigma0)
            Ky = torch.exp(-Dyy / sigma0)
            Kxy = torch.exp(-Dxy / sigma0)

    return h1_mean_var_gram(Kx, Ky, Kxy, is_var_computed, use_1sample_U)


def MMD_L(N1, N2, device, dtype):  # , kernel_mul=2.0, kernel_num=5, fix_sigma=None
    Lii = torch.ones(N1, N1, device=device, dtype=dtype) / N1 / N1
    Ljj = torch.ones(N2, N2, device=device, dtype=dtype) / N2 / N2
    Lij = -1 * torch.ones(N1, N2, device=device, dtype=dtype) / N1 / N2
    Lu = torch.cat([Lii, Lij], 1)
    Ll = torch.cat([Lij.transpose(0, 1), Ljj], 1)
    LM = torch.cat([Lu, Ll], 0)
    return LM

def TST_MMD_median(Fea, N_per, LM, N1, alpha, device, dtype):
    mmd_vector = np.zeros(N_per)
    mmd_value = get_item(MyMMD(Fea, LM, N1, is_median = True),is_cuda)
    count = 0
    Fea = get_item(Fea,is_cuda)
    for i in range(N_per):
        Fea_per = np.random.permutation(Fea)
        Fea_per = MatConvert(Fea_per, device, dtype)
        mmd_vector[i] = get_item(MyMMD(Fea_per, LM, N1, is_median = True),is_cuda)
        if mmd_vector[i] > mmd_value:
            count = count + 1
        if count > np.ceil(N_per * alpha):
            h = 0
            threshold = "NaN"
            break
        else:
            h = 1
    if h == 1:
        S_mmd_vector = np.sort(mmd_vector)
        #        print(np.int(np.ceil(N_per*alpha)))
        threshold = S_mmd_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    return h, threshold, mmd_value.item()

def TST_MMD_adaptive_bandwidth(Fea, N_per, LM, N1, Fea_org, sigma, sigma0, alpha, device, dtype):
    mmd_vector = np.zeros(N_per)
    # mmd_value = MyMMD(Fea, LM, N1).detach().numpy()
    TEMP = MMDu(Fea, N1, Fea_org, sigma, sigma0)
    mmd_value = get_item(TEMP[0],is_cuda)
    Kxyxy = TEMP[2]
    count = 0
    # Fea = get_item(Fea,is_cuda)
    nxy = Fea.shape[0]
    nx = N1

    for r in range(N_per):
        # print r
        ind = np.random.choice(nxy, nxy, replace=False)
        # divide into new X, Y
        indx = ind[:nx]
        # print(indx)
        indy = ind[nx:]
        Kx = Kxyxy[np.ix_(indx, indx)]
        # print(Kx)
        Ky = Kxyxy[np.ix_(indy, indy)]
        Kxy = Kxyxy[np.ix_(indx, indy)]

        TEMP = h1_mean_var_gram(Kx, Ky, Kxy, is_var_computed=False)
        mmd_vector[r] = TEMP[0]
        if mmd_vector[r] > mmd_value:
            count = count + 1
        if count > np.ceil(N_per * alpha):
            h = 0
            threshold = "NaN"
            break
        else:
            h = 1
    if h == 1:
        S_mmd_vector = np.sort(mmd_vector)
        #        print(np.int(np.ceil(N_per*alpha)))
        threshold = S_mmd_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    return h, threshold, mmd_value.item()

def TST_MMD_u(Fea, N_per, LM, N1, Fea_org, sigma, sigma0, alpha,  device, dtype):
    mmd_vector = np.zeros(N_per)
    # mmd_value = MyMMD(Fea, LM, N1).detach().numpy()
    TEMP = MMDu(Fea, N1, Fea_org, sigma, sigma0)
    mmd_value = get_item(TEMP[0], is_cuda)
    Kxyxy = TEMP[2]
    count = 0
    # Fea = get_item(Fea, is_cuda)
    nxy = Fea.shape[0]
    nx = N1

    for r in range(N_per):
        # print r
        ind = np.random.choice(nxy, nxy, replace=False)
        # divide into new X, Y
        indx = ind[:nx]
        # print(indx)
        indy = ind[nx:]
        Kx = Kxyxy[np.ix_(indx, indx)]
        # print(Kx)
        Ky = Kxyxy[np.ix_(indy, indy)]
        Kxy = Kxyxy[np.ix_(indx, indy)]

        TEMP = h1_mean_var_gram(Kx, Ky, Kxy, is_var_computed=False)
        mmd_vector[r] = TEMP[0]
        if mmd_vector[r] > mmd_value:
            count = count + 1
        if count > np.ceil(N_per * alpha):
            h = 0
            threshold = "NaN"
            break
        else:
            h = 1
    if h == 1:
        S_mmd_vector = np.sort(mmd_vector)
        #        print(np.int(np.ceil(N_per*alpha)))
        threshold = S_mmd_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    return h, threshold, mmd_value.item()

def TST_C2ST(pred,y,N_per,alpha):
    STAT_vector = np.zeros(N_per)
    N = len(pred)
    acc_C2ST = pred.eq(y.data.view_as(pred)).cpu().sum().item() * 1.0 / ((N) * 1.0)
    STAT = abs(acc_C2ST - 0.5)
    count = 0
    for r in range(N_per):
        # print r
        ind = np.random.choice(N, N, replace=False)
        # divide into new X, Y
        y_new = y[ind]
        # print(indx)
        acc_C2ST_new = pred.eq(y_new.data.view_as(pred)).cpu().sum().item() * 1.0 / ((N) * 1.0)
        STAT_vector[r] = abs(acc_C2ST_new - 0.5)
    #     if STAT_vector[r] > STAT:
    #         count = count + 1
    #     if count > np.ceil(N_per * alpha):
    #         h = 0
    #         threshold = "NaN"
    #         break
    #     else:
    #         h = 1
    # if h == 1:
    #     S_vector = np.sort(STAT_vector)
    #     #        print(np.int(np.ceil(N_per*(1 - alpha))))
    #     threshold = S_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    S_vector = np.sort(STAT_vector)
    threshold = S_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    if STAT > threshold:
        h=1
    else:
        h=0
    return h, threshold, STAT

def TST_MMD_b(Fea, N_per, LM, N1, alpha, device, dtype):
    mmd_vector = np.zeros(N_per)
    mmd_value = get_item(MyMMD(Fea, LM, N1),is_cuda)
    count = 0
    Fea = get_item(Fea,is_cuda)
    for i in range(N_per):
        Fea_per = np.random.permutation(Fea)
        Fea_per = MatConvert(Fea_per, device, dtype)
        mmd_vector[i] = get_item(MyMMD(Fea_per, LM, N1),is_cuda)
        if mmd_vector[i] > mmd_value:
            count = count + 1
        if count > np.ceil(N_per * alpha):
            h = 0
            threshold = "NaN"
            break
        else:
            h = 1
    if h == 1:
        S_mmd_vector = np.sort(mmd_vector)
        #        print(np.int(np.ceil(N_per*alpha)))
        threshold = S_mmd_vector[np.int(np.ceil(N_per * (1 - alpha)))]
    return h, threshold, mmd_value.item()

def TST_ME(Fea, N1, alpha, is_train, test_locs, gwidth, J = 1, seed = 15):
    Fea = get_item(Fea,is_cuda)
    tst_data = data.TSTData(Fea[0:N1,:], Fea[N1:,:])
    h = 0
    if is_train:
        op = {
            'n_test_locs': J,  # number of test locations to optimize
            'max_iter': 300,  # maximum number of gradient ascent iterations
            'locs_step_size': 1.0,  # step size for the test locations (features)
            'gwidth_step_size': 0.1,  # step size for the Gaussian width
            'tol_fun': 1e-4,  # stop if the objective does not increase more than this.
            'seed': seed + 5,  # random seed
        }
        test_locs, gwidth, info = tst.MeanEmbeddingTest.optimize_locs_width(tst_data, alpha, **op)
        return test_locs, gwidth
    else:
        met_opt = tst.MeanEmbeddingTest(test_locs, gwidth, alpha)
        test_result = met_opt.perform_test(tst_data)
        if test_result['h0_rejected']:
            h = 1
        return h

def TST_SCF(Fea, N1, alpha, is_train, test_freqs, gwidth, J = 1, seed = 15):
    Fea = get_item(Fea,is_cuda)
    tst_data = data.TSTData(Fea[0:N1,:], Fea[N1:,:])
    h = 0
    if is_train:
        op = {'n_test_freqs': J, 'seed': seed, 'max_iter': 300,
              'batch_proportion': 1.0, 'freqs_step_size': 0.1,
              'gwidth_step_size': 0.01, 'tol_fun': 1e-4}
        test_freqs, gwidth, info = tst.SmoothCFTest.optimize_freqs_width(tst_data, alpha, **op)
        return test_freqs, gwidth
    else:
        scf_opt = tst.SmoothCFTest(test_freqs, gwidth, alpha=alpha)
        test_result = scf_opt.perform_test(tst_data)
        if test_result['h0_rejected']:
            h = 1
        return h