from numpyro import optim


def Adam(kwargs):
    step_size = kwargs.pop('lr')
    b1, b2 = kwargs.pop('betas', (0.9, 0.999))
    eps = kwargs.pop('eps', 1.e-8)
    return optim.Adam(step_size=step_size, b1=b1, b2=b2, eps=eps)
