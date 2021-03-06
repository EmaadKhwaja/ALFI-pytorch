import numpy as np
import os

import torch
import torch.nn as nn

import utils.losses as losses
import utils.weights as weights
from models import DataAggregator
from models import RIM
import torch.optim as optim
from random import randint
import time


class ALFINet(nn.Module):
    dict_loss_func = {"mse": losses.loss_mse,
                      "l1": losses.loss_l1,
                      "normal": losses.loss_normal_density}

    dict_weight_func = {"const": weights.weight_const,
                        "oi": weights.weight_oi,
                        "last": weights.weight_last,
                        "exp": weights.weight_exp}

    def __init__(self, arch_config, train_config, simulator, proposal, verbose=3, device="cpu"):
        super().__init__()

        if device is None:
            self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        else:
            self.device = device

        self.verbose = verbose

        self.proposal = proposal
        self.simulator = simulator

        self.loss_func = None
        self.weight_func = None

        self.mu_X = 0
        self.sigma_X = 1
        self.X_train = None
        self.X_test = None

        self.mu_theta = 0
        self.sigma_theta = 1
        self.theta_train = None
        self.theta_test = None

        self.epoch = 0
        self.list_loss_epoch = []
        self.list_rmse_epoch = []
        self.list_val_loss = []
        self.list_val_rmse = []
        self.list_epochs_val = []

        self.nb_iter = train_config['nb_iter']
        self.use_grad = arch_config['use_grad']
        self.st_rim_size = arch_config['RIM']['st_size']

        self.x_data_agg = DataAggregator(simulator.x_dim, arch_config['x_data_agg']).to(self.device)

        self.psi_dim = proposal.psi_dim
        self.theta_dim = simulator.theta_dim

        theta_dim = simulator.theta_dim

        if self.use_grad:
            self.theta_data_agg = DataAggregator(theta_dim*(self.proposal.psi_dim + 1)
                                                 +2*arch_config['x_data_agg']['output_size'],
                                                 arch_config['theta_data_agg']).to(self.device)
        else:
            self.theta_data_agg = DataAggregator(theta_dim + 2 * arch_config['x_data_agg']['output_size'],
                                                 arch_config['theta_data_agg']).to(self.device)

        self.rim = RIM(proposal.psi_dim*theta_dim + arch_config['theta_data_agg']['output_size'],
                       self.st_rim_size, arch_config['RIM']['hidden_size'],
                       proposal.psi_dim*theta_dim, bounded=arch_config['RIM']['bounded']).to(self.device)

        if train_config['loss'] not in ALFINet.dict_loss_func.keys():
            raise ValueError("Invalid loss in argument of RIM: {}".format(train_config['loss']))
        if train_config['weight'] not in ALFINet.dict_weight_func.keys():
            raise ValueError("Invalid weight in argument of RIM: {}".format(train_config['weight']))

        self.loss_func = ALFINet.dict_loss_func[train_config['loss']]
        self.weight_func = ALFINet.dict_weight_func[train_config['weight']]
        self.optimizer = optim.Adam(self.parameters(), lr=2e-4)
        
    def forward_step(self, x_real_encoded, x_gen, theta_gen, st, psi_t, phase="train"):
        """x_real, x_gen: (meta_bs, theta_bs, x_bs, x_dim)
           theta_gen: (meta_bs, theta_bs, theta_dim)"""

        meta_bs, theta_bs, x_bs, x_dim = x_gen.shape

        true_data_encoded = x_real_encoded
        gen_data_encoded = self.x_data_agg(x_gen.contiguous().view(-1, x_bs, x_dim), phase=phase).view(meta_bs, theta_bs, -1)

        # -> (meta_bs, theta_bs, data_agg_output_dim)

        # data_encoded = torch.norm(true_data_encoded - gen_data_encoded, dim=2).unsqueeze(2)**2
        # -> (meta_bs, theta_bs, 1)

        data_encoded = torch.cat((true_data_encoded, gen_data_encoded), dim=2)
        # -> (meta_bs, theta_bs, data_agg_output_dim*2)

        input_theta_agg = torch.cat((data_encoded, theta_gen), dim=2)

        theta_encoded = self.theta_data_agg(input_theta_agg, phase=phase)
        # -> (meta_bs, data_agg_output_dim)

        rim_input = torch.cat((theta_encoded, psi_t), 1)

        if phase == "test":
            rim_input = rim_input.detach()

        return self.rim.forward(rim_input, st)

    def forward(self, X, batch_size_x, batch_size_theta, nb_iter=30, phase="train"):
        """X: (meta_batch_size, nb_x_per_theta, dim_x)"""
        if phase == "train":
            self.sigma_theta, self.mu_theta = self.sigma_theta_train, self.mu_theta_train
            self.sigma_X, self.mu_X = self.sigma_Xtrain, self.mu_Xtrain
        else:
            self.sigma_theta, self.mu_theta = self.sigma_theta_test, self.mu_theta_test
            self.sigma_X, self.mu_X = self.sigma_Xtest, self.mu_Xtest

        theta_dim = self.simulator.theta_dim
        x_dim = self.simulator.x_dim

        st = self.rim.init_hidden(X.shape[0]).to(self.device)
        psi_t = self.proposal.init_psi(X.shape[0], self.simulator)
        list_psi_t = torch.zeros(X.shape[0], nb_iter, self.proposal.psi_dim, self.simulator.theta_dim).to(self.device)

        x_real = X.unsqueeze(1).expand(-1, batch_size_theta, -1, -1).contiguous()
        meta_bs, theta_bs, x_bs, x_dim = x_real.shape
        x_real_encoded = self.x_data_agg(x_real.view(-1, x_bs, x_dim), phase=phase).view(meta_bs, theta_bs, -1)

        for it in range(nb_iter):
            # Generate gen_data_b_size thetas for each sample of the batch
            theta_gen = self.unnormalize(self.proposal.sample(batch_size_theta).contiguous(), self.mu_theta, self.sigma_theta)

            x_gen = self.simulator.forward(theta_gen.view(-1,theta_dim), batch_size_x)
            x_gen = x_gen.view(theta_gen.shape[0], batch_size_theta, batch_size_x, x_dim)

            x_gen = self.normalize(x_gen, self.mu_X, self.sigma_X)[2]

            theta_gen = self.normalize(theta_gen, self.mu_theta, self.sigma_theta)[2]

            if self.use_grad:
                psi_gen = self.proposal.normalized_grad_log(theta_gen.permute(1, 0, 2)) \
                          .permute(2, 0, 1, 3).contiguous().view(X.shape[0], batch_size_theta, -1)

                # Concat theta and its gradient
                theta_gen = torch.cat((theta_gen, psi_gen), 2)
                # theta_gen: (meta_batch_size, batch_size_theta, psi_dim + 1, theta_dim)

            d_psi, st = self.forward_step(x_real_encoded, x_gen, theta_gen, st, psi_t.view(x_real.shape[0], -1), phase=phase)

            psi_t += d_psi.view(x_real.shape[0], self.proposal.psi_dim, self.simulator.theta_dim)
            self.proposal.update_psi(psi_t)
            list_psi_t[:, it, :, :] = psi_t
        #if phase=="test":
        #    list_psi_t = list_psi_t.cpu().detach()
        return list_psi_t

    def backprop_all(self, loss):
        loss.backward()
        self.optimizer.step()
        self.zero_grad()

    def backprop(self, loss):
        loss.backward()
        self.theta_data_agg.optimizer.step()
        self.x_data_agg.optimizer.step()
        self.rim.optimizer.step()

        self.rim.zero_grad()
        self.x_data_agg.zero_grad()
        self.theta_data_agg.zero_grad()

    def loss(self, theta, list_psi_t):
        loss_t = self.loss_func(theta, list_psi_t)
        return self.weight_func(loss_t, device=self.device)

    def train(self, train_config):
        # super().train(True)
        meta_batch_size = train_config['meta_batch_size']
        nb_theta = train_config['nb_theta']
        nb_epochs = train_config['nb_epochs']
        test_every = train_config['test_every']
        save_every = train_config['save_every']
        nb_iter = train_config['nb_iter']

        if self.epoch == 0:
            theta_train, X_train = self.simulator.get_data(nb_theta, train_config['nb_x_per_theta'])

            self.mu_Xtrain, self.sigma_Xtrain, self.X_train = self.normalize(X_train.contiguous().view(-1, X_train.shape[2]))
            self.X_train = self.X_train.view(X_train.shape[0], X_train.shape[1], X_train.shape[2]).to(self.device)
            self.mu_Xtrain, self.sigma_Xtrain = self.mu_Xtrain.to(self.device), self.sigma_Xtrain.to(self.device)

            self.mu_theta_train, self.sigma_theta_train, self.theta_train = self.normalize(theta_train)
            self.mu_theta_train, self.sigma_theta_train, self.theta_train = self.mu_theta_train.to(self.device), \
                                                                            self.sigma_theta_train.to(self.device), \
                                                                            self.theta_train.to(self.device)

        for self.epoch in range(self.epoch, nb_epochs):
            if self.verbose >= 2:
                print("\n~~~~~~~~~~~~~~~~~~~~~~ Epoch {} ~~~~~~~~~~~~~~~~~~~~~~\n".format(self.epoch))

            idx = torch.randperm(self.theta_train.size()[0])
            theta_train, X_train = self.theta_train[idx], self.X_train[idx]  # shuffle batch

            loss_epoch = 0
            rmse_epoch = 0

            # if self.verbose >= 2:
            #     list_batches = trange(0, nb_theta, meta_batch_size)
            # else:
            #     list_batches = range(0, nb_theta, meta_batch_size)
            list_batches = range(0, nb_theta, meta_batch_size)

            for i_begin_batch in list_batches:
                i_end_batch = i_begin_batch + meta_batch_size

                if i_end_batch > nb_theta:
                    break

                # thetas and associated X of the meta batch
                thetas = theta_train[i_begin_batch:i_end_batch]
                X = X_train[i_begin_batch:i_end_batch]
                list_psi_t = self.forward(X, train_config['batch_size_x'],
                                          train_config['batch_size_theta'],
                                          self.nb_iter)
                loss_batch = 0
                mse_batch = 0

                for i in range(meta_batch_size):
                    loss_batch += self.loss(thetas[i], list_psi_t[i]) / meta_batch_size
                    mse_batch += weights.weight_last(
                        losses.loss_mse(thetas[i], list_psi_t[i])) / meta_batch_size

                self.backprop(loss_batch)

                loss_epoch += loss_batch.cpu().detach().numpy()
                rmse_epoch += torch.sqrt(mse_batch).cpu().detach().numpy()

                if self.verbose >= 2 and i_begin_batch > 0:
                    # list_batches.set_description("Loss = {:04f}, RMSE = {:04f}"
                    # .format(loss_epoch / (i_begin_batch // meta_batch_size + 1),
                    #         rmse_epoch / (i_begin_batch // meta_batch_size + 1)))

                    print("Batch: {} / {} −− Loss: {:05f} −− RMSE: {:05f} - nb_threads: {}"
                          .format(i_begin_batch // meta_batch_size, nb_theta // meta_batch_size,
                                  loss_epoch / (i_begin_batch // meta_batch_size + 1),
                                  rmse_epoch / (i_begin_batch // meta_batch_size + 1),
                                  torch.get_num_threads()
                                  ), end="\r")

            loss_epoch /= (nb_theta // meta_batch_size)
            rmse_epoch /= (nb_theta // meta_batch_size)
            
            if self.verbose >= 2:
                print("")

            self.list_loss_epoch.append(loss_epoch)
            self.list_rmse_epoch.append(rmse_epoch)

            if self.epoch != 0 and (self.epoch % save_every == 0 or self.epoch % test_every == 0):
                return

        self.epoch += 1
        return self.list_loss_epoch, self.list_rmse_epoch

    def test(self, test_config, nb_theta=None, regenerate=False):

        mb_size = 64
        if nb_theta is None:
            nb_theta = test_config['nb_theta']

        if regenerate or self.theta_test is None or self.X_test is None or nb_theta > self.theta_test.shape[0]:
            theta_test, X_test = self.simulator.get_data(nb_theta,
                                                         test_config['nb_x_per_theta'])
            if nb_theta > 1000:
                self.mu_Xtest, self.sigma_Xtest, self.X_test = self.normalize(
                    X_test.to(self.device).contiguous().view(-1, X_test.shape[2]))
                self.mu_theta_test, self.sigma_theta_test, self.theta_test = self.normalize(theta_test.to(self.device),
                                                                                            self.mu_theta_train,
                                                                                            self.sigma_theta_train)
            else:
                self.mu_Xtest, self.sigma_Xtest, self.X_test = self.normalize(
                    X_test.to(self.device).contiguous().view(-1, X_test.shape[2])
                    , self.mu_Xtrain, self.sigma_Xtrain)
                self.mu_theta_test, self.sigma_theta_test, self.theta_test = self.normalize(theta_test.to(self.device))
            self.X_test = self.X_test.view(X_test.shape[0], X_test.shape[1], X_test.shape[2]).to(self.device)

        with torch.no_grad():
            self.X_test.requires_grad = False
            list_psi_t = torch.zeros(nb_theta, test_config['nb_iter'], self.psi_dim, self.theta_dim).to(self.device)
            for i in range(0, nb_theta, mb_size):
                i_end = i + mb_size
                if i_end >= nb_theta:
                    i_end = nb_theta

                list_psi_t[i:i_end, :, :, :] = self.forward(self.X_test[i:i_end], test_config['batch_size_x'], mb_size,
                                              test_config['nb_iter'], phase="test")
            loss_test = 0

            for i in range(nb_theta):
                loss_test += self.loss(self.theta_test[i], list_psi_t[i]).cpu().detach() / nb_theta

        return list_psi_t, self.theta_test[:nb_theta], loss_test

    def save(self, model_dir, values_dir):
        # General informations
        if not os.path.exists(os.path.join(values_dir, "X_train.pt")):
            torch.save(self.X_train.cpu(), os.path.join(values_dir, "X_train.pt"))
            torch.save(self.theta_train.cpu(), os.path.join(values_dir, "theta_train.pt"))
            torch.save(self.mu_X.cpu(), os.path.join(values_dir, "mu_X.pt"))
            torch.save(self.sigma_X.cpu(), os.path.join(values_dir, "sigma_X.pt"))
            torch.save(self.mu_theta.cpu(), os.path.join(values_dir, "mu_theta.pt"))
            torch.save(self.sigma_theta.cpu(), os.path.join(values_dir, "sigma_theta.pt"))

            torch.save(self.mu_theta_train.cpu(), os.path.join(values_dir, "mu_theta_train.pt"))
            torch.save(self.sigma_theta_train.cpu(), os.path.join(values_dir, "sigma_theta_train.pt"))
            torch.save(self.mu_Xtrain.cpu(), os.path.join(values_dir, "mu_Xtrain.pt"))
            torch.save(self.sigma_Xtrain.cpu(), os.path.join(values_dir, "sigma_Xtrain.pt"))


        if not os.path.exists(os.path.join(values_dir, "X_test.pt")) and self.X_test is not None:
            torch.save(self.X_test.cpu(), os.path.join(values_dir, "X_test.pt"))
            torch.save(self.theta_test.cpu(), os.path.join(values_dir, "theta_test.pt"))
            torch.save(self.mu_theta_test.cpu(), os.path.join(values_dir, "mu_theta_test.pt"))
            torch.save(self.sigma_theta_test.cpu(), os.path.join(values_dir, "sigma_theta_test.pt"))
            torch.save(self.mu_Xtest.cpu(), os.path.join(values_dir, "mu_Xtest.pt"))
            torch.save(self.sigma_Xtest.cpu(), os.path.join(values_dir, "sigma_Xtest.pt"))

        # Current informations
        torch.save(self.state_dict(), os.path.join(model_dir, "last-model.pt"))

        for param_group in self.optimizer.param_groups:
            lr = param_group['lr']
        curr_state = {
            'epoch': self.epoch,
            'list_loss_epoch': self.list_loss_epoch,
            'list_rmse_epoch': self.list_rmse_epoch,
            'list_val_loss': self.list_val_loss,
            'list_val_rmse': self.list_val_rmse,
            'list_epochs_val': self.list_epochs_val,
            'lr': lr,
            'nb_iter': self.nb_iter

        }
        torch.save(curr_state, os.path.join(values_dir, "last-state.pt"))

    def load(self, model_dir, values_dir):
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.X_train = torch.load(os.path.join(values_dir, "X_train.pt")).to(self.device)
        self.theta_train = torch.load(os.path.join(values_dir, "theta_train.pt")).to(self.device)
        self.mu_X = torch.load(os.path.join(values_dir, "mu_X.pt")).to(self.device)
        self.sigma_X = torch.load(os.path.join(values_dir, "sigma_X.pt")).to(self.device)
        self.mu_theta = torch.load(os.path.join(values_dir, "mu_theta.pt")).to(self.device)
        self.sigma_theta = torch.load(os.path.join(values_dir, "sigma_theta.pt")).to(self.device)
        self.sigma_Xtrain = torch.load(os.path.join(values_dir, "sigma_Xtrain.pt")).to(self.device)
        self.mu_Xtrain = torch.load(os.path.join(values_dir, "mu_Xtrain.pt")).to(self.device)
        self.sigma_theta_train = torch.load(os.path.join(values_dir, "sigma_theta_train.pt")).to(self.device)
        self.mu_theta_train = torch.load(os.path.join(values_dir, "mu_theta_train.pt")).to(self.device)

        if os.path.exists(os.path.join(values_dir, "X_test.pt")):
            self.X_test = torch.load(os.path.join(values_dir, "X_test.pt")).to(self.device)
            self.theta_test = torch.load(os.path.join(values_dir, "theta_test.pt")).to(self.device)
            self.sigma_theta_test = torch.load(os.path.join(values_dir, "sigma_theta_test.pt")).to(self.device)
            self.mu_theta_test = torch.load(os.path.join(values_dir, "mu_theta_test.pt")).to(self.device)
            self.mu_Xtest = torch.load(os.path.join(values_dir, "mu_Xtest.pt")).to(self.device)
            self.sigma_Xtest = torch.load(os.path.join(values_dir, "sigma_Xtest.pt")).to(self.device)

        self.load_state_dict(torch.load(os.path.join(model_dir, "last-model.pt")))
        state = torch.load(os.path.join(values_dir, "last-state.pt"))

        self.epoch = state['epoch'] + 1
        self.list_loss_epoch = state['list_loss_epoch']
        self.list_rmse_epoch = state['list_rmse_epoch']
        self.list_val_loss = state['list_val_loss']
        self.list_val_rmse = state['list_val_rmse']
        self.list_epochs_val = state['list_epochs_val']
        self.nb_iter = state['nb_iter']
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = state['lr']

    @staticmethod
    def normalize(data, mu=None, sigma=None):
        if mu is None:
            mu = data.mean(0)
        if sigma is None:
            sigma = data.std(0)
            sigma[sigma == 0] = 1
        return mu, sigma, (data - mu)/sigma

    @staticmethod
    def unnormalize(data, mu, sigma):
        return data * sigma + mu
