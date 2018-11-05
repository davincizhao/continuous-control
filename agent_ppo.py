import torch
import numpy as np
import torch.optim as optim
import torch.nn as nn
import os
import pdb

class Agent():
    def __init__(self, env,
                 policy, timesteps=200, gamma=0.99, epochs=10, gae_tau=0.95,
                 batch_size=32, ratio_clip=0.2, lrate=1e-3, beta=0.01, gradient_clip=5):
        self.timesteps = timesteps
        self.env = env
        self.policy = policy
        self.gamma = gamma
        self.epochs = epochs
        self.batch_size = batch_size
        self.ratio_clip = ratio_clip
        self.lrate = lrate
        self.gradient_clip = gradient_clip
        self.beta = beta
        self.gae_tau = gae_tau

        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.state = self.env.reset()
        self.opt = optim.RMSprop(policy.parameters(), lr=lrate)
        self.rewards = np.zeros(self.num_agents)
        self.episodes_reward = []
        self.steps = 0

    @property
    def num_agents(self):
        return self.env.num_agents

    @property
    def action_size(self):
        return self.env.action_size

    def save(self, path):
        directory = os.path.dirname(path)
        if not os.path.exists(directory):
            os.mkdir(directory)
        torch.save(self.policy.state_dict(), path)

    def tensor_from_np(self, x):
        return torch.from_numpy(x).float().to(self.device)

    def get_batch(self, states, actions, old_log_probs, returns, advs):
        length = states.shape[0]
        idx = np.random.permutation(length)
        # only full batch
        for i in range(length // self.batch_size):
            rge = idx[i*self.batch_size:(i+1)*self.batch_size]
            yield (
                states[rge], actions[rge], old_log_probs[rge], returns[rge], advs[rge].squeeze(1)
                )

    def step(self):
        trajectory_raw = []
        for _ in range(self.timesteps):

            state = self.tensor_from_np(self.state)
            action, log_p, _, value = self.policy(state)

            log_p = log_p.detach().cpu().numpy()
            value = value.detach().squeeze(1).cpu().numpy()
            action = action.detach().cpu().numpy()

            next_state, reward, done = self.env.step(action)
            self.rewards += reward

            # check if some episodes are done
            for i, d in enumerate(done):
                if d:
                    self.episodes_reward.append(self.rewards[i])
                    self.rewards[i] = 0

            trajectory_raw.append((state, action, reward, log_p, value, 1-done))
            self.state = next_state

        next_value = self.policy(self.tensor_from_np(self.state))[-1].detach().squeeze(1)
        trajectory_raw.append((state, None, None, None, next_value.cpu().numpy(), None))
        trajectory = [None] * (len(trajectory_raw)-1)
        # process raw trajectories
        # calculate advantages and returns
        advs = torch.zeros(self.num_agents, 1).to(self.device)
        R = next_value

        for i in reversed(range(len(trajectory_raw)-1)):

            states, actions, rewards, log_probs, values, dones = trajectory_raw[i]
            actions, rewards, dones, values, next_values, log_probs = map(
                lambda x: torch.tensor(x).float().to(self.device),
                (actions, rewards, dones, values, trajectory_raw[i+1][-2], log_probs)
            )
            R = rewards + self.gamma * R * dones
            # without gae, advantage is calculated as:
            #advs = R[:,None] - values[:,None]
            td_errors = rewards + self.gamma * dones * next_values - values
            advs = advs * self.gae_tau * self.gamma * dones[:, None] + td_errors[:, None]
            # with gae
            trajectory[i] = (states, actions, log_probs, R, advs)

        states, actions, old_log_probs, returns, advs = map(
            lambda x: torch.cat(x, dim=0), zip(*trajectory)
            )

        # normalize advantages
        advs = (advs - advs.mean())  / (advs.std() + 1.0e-10)

        # train policy with random batchs of accumulated trajectories
        for _ in range(self.epochs):

            for states_b, actions_b, old_log_probs_b, returns_b, advs_b in \
                self.get_batch(states, actions, old_log_probs, returns, advs):

                # get updated values from policy
                _, new_log_probs_b, entropy_b, values_b = self.policy(states_b, actions_b)

                # ratio for clipping
                ratio = (new_log_probs_b - old_log_probs_b).exp()

                # Clipped function
                clip = torch.clamp(ratio, 1-self.ratio_clip, 1+self.ratio_clip)
                clipped_surrogate = torch.min(ratio*advs_b.unsqueeze(1), clip*advs_b.unsqueeze(1))

                actor_loss = -torch.mean(clipped_surrogate) - self.beta * entropy_b.mean()
                critic_loss = 0.5 * (returns_b - values_b).pow(2).mean()

                self.opt.zero_grad()
                (actor_loss + critic_loss).backward()
                nn.utils.clip_grad_norm_(self.policy.parameters(), self.gradient_clip)
                self.opt.step()

                self.steps += self.batch_size
