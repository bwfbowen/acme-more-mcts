# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A Monte Carlo Tree Search implementation."""

import dataclasses
from typing import Callable, Dict, Tuple, List

from acme.agents.tf.mcts import models
from acme.agents.tf.mcts import types
import numpy as np


@dataclasses.dataclass
class Node:
  """A MCTS node."""

  reward: float = 0.
  visit_count: int = 0
  terminal: bool = False
  prior: float = 1.
  total_value: float = 0.
  children: Dict[types.Action, 'Node'] = dataclasses.field(default_factory=dict)

  def expand(self, prior: np.ndarray):
    """Expands this node, adding child nodes."""
    assert prior.ndim == 1  # Prior should be a flat vector.
    for a, p in enumerate(prior):
      self.children[a] = Node(prior=p)

  @property
  def value(self) -> types.Value:  # Q(s, a)
    """Returns the value from this node."""
    if self.visit_count:
      return self.total_value / self.visit_count
    return 0.

  @property
  def children_visits(self) -> np.ndarray:
    """Return array of visit counts of visited children."""
    return np.array([c.visit_count for c in self.children.values()])

  @property
  def children_values(self) -> np.ndarray:
    """Return array of values of visited children."""
    return np.array([c.value for c in self.children.values()])


@dataclasses.dataclass
class SampledNode:
  """A MCTS node."""
  prior: float = 1.
  visit_count: int = 0
  reward: float = 0.
  terminal: bool = False
  total_value: float = 0.
  sub_nodes: Dict[types.Dim, 'SubNode'] = dataclasses.field(default_factory=dict)
  children: Dict[types.Dim, Dict[types.Action, 'SubNode']] = dataclasses.field(default_factory=dict)
  sampled_action: Dict[types.Dim, Dict[int, types.Action]] = dataclasses.field(default_factory=dict)
  visited: Dict[types.Action, 'SampledNode'] = dataclasses.field(default_factory=dict)

  def expand(self, actions: List[types.Action], priors: List[types.Probs]):
    """Expands this node, adding child nodes."""
     # Priors should be a list of prior
    for dim, (action, prior) in enumerate(zip(actions, priors)):
      self.children[dim], self.sampled_action[dim] = {}, {}
      for idx, (a, p) in enumerate(zip(action, prior)):
        self.children[dim][idx] = SubNode(prior=p)
        self.sampled_action[dim][idx] = a 
    
  def update_total_value(self, ret: float):
    self.total_value += ret 
    for dim in self.sub_nodes:
      self.sub_nodes[dim].total_value += ret
  
  def update_visit_count(self, increment: int):
    self.visit_count += increment
    for dim in self.sub_nodes:
      self.sub_nodes[dim].visit_count += increment

  @property
  def value(self) -> types.Value:  # Q(s, a)
    """Returns the value from this node."""
    visit_count = self.visit_count
    if visit_count:
      return self.total_value / visit_count
    return 0.

  @property
  def children_visits(self) -> List[np.ndarray]:
    """Return array of visit counts of visited children for each dimension."""
    return [np.array([c.visit_count for c in self.children[dim].values()]) for dim in self.children]

  @property
  def children_values(self) -> List[np.ndarray]:
    """Return array of values of visited children for each dimension."""
    return [np.array([c.value for c in self.children[dim].values()]) for dim in self.children] 
  
  @classmethod
  def from_sub_nodes(cls, sub_nodes: List['SubNode']):
    _sub_nodes = {}
    for dim, sub_node in enumerate(sub_nodes):
      _sub_nodes[dim] = sub_node
    return cls(sub_nodes=_sub_nodes)


@dataclasses.dataclass
class SubNode:
  """A MCTS sub node for one dimension."""
  reward: float = 0.
  visit_count: int = 0
  terminal: bool = False
  prior: float = 1.
  total_value: float = 0.

  @property
  def value(self) -> types.Value:  # Q(s, a)
    """Returns the value from this node."""
    visit_count = self.visit_count
    if visit_count:
      return self.total_value / visit_count
    return 0.
  

SearchPolicy = Callable[[Node], types.Action]
FactoredSearchPolicy = Callable[[SampledNode, int], types.Action]
SamplePolicy = Callable[[types.Probs], Tuple[types.Action, types.Probs]]


def mcts(
    observation: types.Observation,
    model: models.Model,
    search_policy: SearchPolicy,
    evaluation: types.EvaluationFn,
    num_simulations: int,
    num_actions: int,
    discount: float = 1.,
    dirichlet_alpha: float = 1,
    exploration_fraction: float = 0.,
) -> Node:
  """Does Monte Carlo tree search (MCTS), AlphaZero style."""

  # Evaluate the prior policy for this state.
  prior, value = evaluation(observation)
  assert prior.shape == (num_actions,)

  # Add exploration noise to the prior.
  noise = np.random.dirichlet(alpha=[dirichlet_alpha] * num_actions)
  prior = prior * (1 - exploration_fraction) + noise * exploration_fraction

  # Create a fresh tree search.
  root = Node()
  root.expand(prior)

  # Save the model state so that we can reset it for each simulation.
  model.save_checkpoint()
  for _ in range(num_simulations):
    # Start a new simulation from the top.
    trajectory = [root]
    node = root

    # Generate a trajectory.
    timestep = None
    while node.children:
      # Select an action according to the search policy.
      action = search_policy(node)

      # Point the node at the corresponding child.
      node = node.children[action]

      # Step the simulator and add this timestep to the node.
      timestep = model.step(action)
      node.reward = timestep.reward or 0.
      node.terminal = timestep.last()
      trajectory.append(node)

    if timestep is None:
      raise ValueError('Generated an empty rollout; this should not happen.')

    # Calculate the bootstrap for leaf nodes.
    if node.terminal:
      # If terminal, there is no bootstrap value.
      value = 0.
    else:
      # Otherwise, bootstrap from this node with our value function.
      prior, value = evaluation(timestep.observation)

      # We also want to expand this node for next time.
      node.expand(prior)

    # Load the saved model state.
    model.load_checkpoint()

    # Monte Carlo back-up with bootstrap from value function.
    ret = value
    while trajectory:
      # Pop off the latest node in the trajectory.
      node = trajectory.pop()

      # Accumulate the discounted return
      ret *= discount
      ret += node.reward

      # Update the node.
      node.total_value += ret
      node.visit_count += 1

  return root


def sampled_mcts(
    observation: types.Observation,
    model: models.Model,
    search_policy: FactoredSearchPolicy,
    sample_policy: SamplePolicy, 
    evaluation: types.EvaluationFn,
    num_simulations: int,
    discount: float = 1.,
    dirichlet_alpha: float = 1,
    exploration_fraction: float = 0.,
    action_converter: types.ActionConverter = lambda x: x,
) -> SampledNode:
  """Does Monte Carlo tree search (MCTS), AlphaZero style."""

  # Evaluate the prior policy for this state.
  prior, value = evaluation(observation)
  assert prior.ndim == 2

  sampled_actions, priors = sample_policy(prior)
  # Add exploration noise to the prior.
  num_dimensions = len(sampled_actions)
  for dim in range(num_dimensions):
    noise = np.random.dirichlet(alpha=[dirichlet_alpha] * len(sampled_actions[dim]))
    priors[dim] = priors[dim] * (1 - exploration_fraction) + noise * exploration_fraction

  # Create a fresh tree search.
  root = SampledNode()
  root.expand(sampled_actions, priors)

  # Save the model state so that we can reset it for each simulation.
  model.save_checkpoint()
  for _ in range(num_simulations):
    # Start a new simulation from the top.
    trajectory = [root]
    node = root

    # Generate a trajectory.
    timestep = None
    while node.children:
      # Select an action according to the factored search policy.
      action = tuple([search_policy(node, dim) for dim in range(num_dimensions)])
      sampled_action = [node.sampled_action[dim][a] for dim, a in enumerate(action)]
      
      # Point the node at the corresponding child.
      if action in node.visited:
        node = node.visited[action]
      else:
        tmp = SampledNode.from_sub_nodes([node.children[dim][action[dim]] for dim in range(num_dimensions)])
        node.visited[action] = tmp 
        node = tmp 
      
      # Step the simulator and add this timestep to the node.
      timestep = model.step(action_converter(sampled_action))
      node.reward = timestep.reward or 0.
      node.terminal = timestep.last()
      trajectory.append(node)
    
    if timestep is None:
      raise ValueError('Generated an empty rollout; this should not happen.')

    # Calculate the bootstrap for leaf nodes.
    if node.terminal:
      # If terminal, there is no bootstrap value.
      value = 0.
    else:
      # Otherwise, bootstrap from this node with our value function.
      prior, value = evaluation(timestep.observation)

      sampled_actions, priors = sample_policy(prior)

      # We also want to expand this node for next time.
      node.expand(sampled_actions, priors)

    # Load the saved model state.
    model.load_checkpoint()

    # Monte Carlo back-up with bootstrap from value function.
    ret = value
    while trajectory:
      # Pop off the latest node in the trajectory.
      node = trajectory.pop()

      # Accumulate the discounted return
      ret *= discount
      ret += node.reward

      # Update the node.
      node.update_total_value(ret)
      node.update_visit_count(1)
      
  return root  


def bfs(node: Node) -> types.Action:
  """Breadth-first search policy."""
  visit_counts = np.array([c.visit_count for c in node.children.values()])
  return argmax(-visit_counts)


def puct(node: Node, ucb_scaling: float = 1.) -> types.Action:
  """PUCT search policy, i.e. UCT with 'prior' policy."""
  # Action values Q(s,a).
  value_scores = np.array([child.value for child in node.children.values()])
  check_numerics(value_scores)

  # Policy prior P(s,a).
  priors = np.array([child.prior for child in node.children.values()])
  check_numerics(priors)

  # Visit ratios.
  visit_ratios = np.array([
      np.sqrt(node.visit_count) / (child.visit_count + 1)
      for child in node.children.values()
  ])
  check_numerics(visit_ratios)

  # Combine.
  puct_scores = value_scores + ucb_scaling * priors * visit_ratios
  return argmax(puct_scores)


def factored_puct(node: SampledNode, dim: int, ucb_scaling: float = 1., ) -> types.Action:
  """PUCT search policy, i.e. UCT with 'prior' policy."""
  # Action values Q(s,a).
  value_scores = np.array([child.value for child in node.children[dim].values()])
  check_numerics(value_scores)

  # Policy prior P(s,a).
  priors = np.array([child.prior for child in node.children[dim].values()])
  check_numerics(priors)

  # Visit ratios.
  visit_ratios = np.array([
      np.sqrt(node.visit_count) / (child.visit_count + 1)
      for child in node.children[dim].values()
  ])
  check_numerics(visit_ratios)

  # Combine.
  puct_scores = value_scores + ucb_scaling * priors * visit_ratios
  return argmax(puct_scores)


def visit_count_policy(root: Node, temperature: float = 1.) -> types.Probs:
  """Probability weighted by visit^{1/temp} of children nodes."""
  visits = root.children_visits
  if np.sum(visits) == 0:  # uniform policy for zero visits
    visits += 1
  rescaled_visits = visits**(1 / temperature)
  probs = rescaled_visits / np.sum(rescaled_visits)
  check_numerics(probs)

  return probs


def factored_visit_count_policy(root: SampledNode, temperature: float = 1.) -> List[types.Probs]:
  """Probability weighted by visit^{1/temp} of children nodes for given dimension."""
  dim_visits = root.children_visits
  dim_probs = []
  for visits in dim_visits:
    if np.sum(visits) == 0:
      visits += 1
    rescaled_visits = visits**(1 / temperature)
    probs = rescaled_visits / np.sum(rescaled_visits)
    check_numerics(probs)
    dim_probs.append(probs)

  return dim_probs


def argmax(values: np.ndarray) -> types.Action:
  """Argmax with random tie-breaking."""
  check_numerics(values)
  max_value = np.max(values)
  return np.int32(np.random.choice(np.flatnonzero(values == max_value)))


def check_numerics(values: np.ndarray):
  """Raises a ValueError if any of the inputs are NaN or Inf."""
  if not np.isfinite(values).all():
    raise ValueError('check_numerics failed. Inputs: {}. '.format(values))
