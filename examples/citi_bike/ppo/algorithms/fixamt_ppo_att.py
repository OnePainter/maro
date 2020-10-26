import torch
from torch import nn
from torch import optim
from torch.distributions import Categorical
import numpy as np
import os
from examples.citi_bike.ppo.models.homo_gnn import LinearBackend
from examples.citi_bike.ppo.models.transformer_policy_with_separate_critic import AttTransPolicy
from copy import deepcopy
from examples.citi_bike.ppo.utils import batchize, from_numpy, obs_to_torch
from maro.utils import Logger, LogFormat
from torch.utils.tensorboard import SummaryWriter

epoch_count = 0
itr_count = 0


class AttGnnPPO:
    def __init__(self, node_dim, channel_cnt, graph_size, log_pth, device="cuda:0", **kargs):
        self.device = torch.device(device)
        self.emb_dim = kargs["emb_dim"]
        self.channel_cnt = channel_cnt
        self.neighbor_cnt = kargs["neighbor_cnt"]
        self.per_graph_size = graph_size
        self.gamma = kargs["gamma"]
        self.temporal_gnn = LinearBackend(node_dim, out_dim=self.emb_dim, channel_cnt=self.channel_cnt)
        self.policy = AttTransPolicy(self.emb_dim, self.neighbor_cnt, graph_size)
        self._logger = Logger(tag="model", format_=LogFormat.simple,
                              dump_folder=log_pth, dump_mode="w", auto_timestamp=False)
        tensorboard_pth = log_pth + "/tensorboard"
        if not os.path.exists(tensorboard_pth):
            os.makedirs(tensorboard_pth)
        self.writer = SummaryWriter(tensorboard_pth + "/citibike_trans")

        self.old_policy = deepcopy(self.policy)
        self.old_temporal_gnn = deepcopy(self.temporal_gnn)

        self.policy = self.policy.to(device=self.device)
        self.old_policy = self.old_policy.to(device=self.device)
        self.old_policy.eval()
        self.temporal_gnn = self.temporal_gnn.to(device=self.device)
        self.old_temporal_gnn = self.old_temporal_gnn.to(device=self.device)
        self.old_temporal_gnn.eval()

        # optimizer
        self.temporal_gnn_opt = optim.Adam(self.temporal_gnn.parameters(), lr=3e-4)
        self.policy_opt = optim.Adam(self.policy.parameters(), lr=3e-4)

        # loss
        self.mse_loss = nn.MSELoss()
        self.K_epochs = 4
        self.eps_clip = 0.2

    def batchize_exp(self, batch):
        if (not batch):
            return {}

        if isinstance(batch[0]["a"], tuple):
            a = np.hstack([e["a"][0] for e in batch])
        else:
            # a.shape: [2, action_cnt]
            a = np.hstack([e["a"] for e in batch])

        # state
        s = batchize([e["obs"] for e in batch])
        s_ = batchize([e["obs_"] for e in batch])
        tot_r = np.array([np.sum(e["r"]) for e in batch])
        r = np.hstack([np.array(e["r"]) for e in batch])
        gamma = np.hstack([np.array(e["gamma"]) for e in batch])

        rlt = {
            "a": a,
            "s": s,
            "s_": s_,
            "r": r,
            "tot_r": tot_r,
            "gamma": gamma,
        }
        # supplement is handled by each algorithm (like GnnddPG), rather than outside.
        if "supplement" in batch[0]:
            rlt["supplement"] = [e["supplement"] for e in batch]
        if "self_r" in batch[0]:
            rlt["self_r"] = np.hstack([np.array(e["self_r"]) for e in batch])
        return rlt

    def act(self, obs):
        '''
        forward gnn and policy to get action
        '''
        with torch.no_grad():
            x, edge_idx_list, action_edge_idx, actual_amount, per_graph_size = obs_to_torch(obs, self.device)
            emb = self.old_temporal_gnn(x, edge_idx_list)
            choice, cnt, att = self.old_policy(emb, action_edge_idx, actual_amount)
            return choice.cpu().numpy(), cnt.cpu().numpy(),\
                {"choice_att": att, "att_prob": torch.log(att[0, choice]).cpu().numpy()}

    def grad(self, batch):
        global epoch_count
        global itr_count

        batch = self.batchize_exp(batch)
        # Monte Carlo estimate of state rewards:
        rewards = from_numpy(torch.FloatTensor, self.device, batch["r"])[0].reshape(-1, self.per_graph_size)
        # print("reward mean",rewards.mean())
        self.writer.add_scalar("Reward\\", rewards.mean(), epoch_count)
        # rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-5)
        tot_gamma = from_numpy(torch.FloatTensor, self.device, batch["gamma"])[0]
        print("reward shape", rewards.shape)
        print("total gamma shape", tot_gamma.shape)
        # normalize to a reasonable scope
        gamma = tot_gamma.reshape(-1, 1).repeat(1, self.per_graph_size)

        x, edge_idx_list, action_edge_idx, actual_amount, per_graph_size = obs_to_torch(batch["s"], self.device)
        x_, edge_idx_list_, action_edge_idx_, actual_amount_, per_graph_size_ = obs_to_torch(batch["s_"], self.device)

        # convert list to tensor
        old_actions = from_numpy(torch.FloatTensor, self.device, batch["a"])[0].reshape(2, -1)
        supplement = self.supplement2torch(batch["supplement"]).float()
        old_logprobs = supplement.reshape(-1)
        loss_ret = []

        # Optimize policy for K epochs:
        for _ in range(self.K_epochs):
            # def evaluate(self, obs, mask, actions):
            ts_emb = self.temporal_gnn(x, edge_idx_list)
            ts_emb_ = self.old_temporal_gnn(x_, edge_idx_list_)
            # action_p is a tuple
            choice, cnt, att = self.policy(ts_emb, action_edge_idx, actual_amount)
            att_dist = Categorical(att.reshape(-1, self.neighbor_cnt + 1))
            action_logprobs = att_dist.log_prob(old_actions[0].reshape(-1))
            att_entropy = att_dist.entropy()

            state_values = self.policy.value(ts_emb)
            state_values_ = self.old_policy.value(ts_emb_).detach()

            # Finding the ratio (pi_theta / pi_theta__old):
            ratios = torch.exp(action_logprobs - old_logprobs.detach())
            # Finding Surrogate Loss:
            rewards = rewards.float()
            # state_values = state_values.reshape((-1,self.batch_size))
            advantages = rewards + gamma * state_values_ - state_values.detach()
            advantages = advantages.sum(-1)
            surr1 = ratios * advantages
            surr2 = torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * advantages
            ploss = - torch.min(surr1, surr2)
            mloss = self.mse_loss(state_values, rewards + gamma * state_values_)
            loss = ploss + mloss - 0.01 * att_entropy

            print("advantage", advantages.mean())
            print("mse loss", mloss.mean())
            self.writer.add_scalar("policy loss\\", ploss.mean(), itr_count)
            self.writer.add_scalar("mse loss\\", mloss.mean(), itr_count)
            self.writer.add_scalar("entropy\\", att_entropy.mean(), itr_count)

            # take gradient step
            self.temporal_gnn_opt.zero_grad()
            self.policy_opt.zero_grad()
            loss.mean().backward()
            self.temporal_gnn_opt.step()
            self.policy_opt.step()

            loss_ret.append(loss.mean())
            itr_count += 1

        self.old_policy.load_state_dict(self.policy.state_dict())
        self.old_temporal_gnn.load_state_dict(self.temporal_gnn.state_dict())
        self.writer.add_scalar("Loss\\", sum(loss_ret) / len(loss_ret), epoch_count)
        epoch_count += 1

    def supplement2torch(self, sup):
        tmp = []
        for sup_i in sup:
            tmp.append(sup_i["att_prob"].reshape(-1))
        rlt = torch.from_numpy(np.vstack(tmp)).to(self.device)
        return rlt

    def save(self, pth):
        torch.save([self.temporal_gnn, self.policy], pth)