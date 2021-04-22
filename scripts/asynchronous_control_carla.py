#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from os.path import join, dirname
sys.path.insert(0, join(dirname(__file__), '../'))

import simulator
simulator.load('/home/wang/CARLA_0.9.9.4')
import carla

from simulator import config, set_weather, add_vehicle
from simulator.sensor_manager import SensorManager
from utils.navigator_sim import get_nav
from learning.models import GeneratorUNet
from learning.model import Generator
#from learning.path_model import ModelGRU

from ff_collect_pm_data import sensor_dict
from utils.collect_ipm import InversePerspectiveMapping
from utils.carla_sensor import Sensor, CarlaSensorMaster
#from utils.train_utils import get_diff_tf
from utils import debug
from utils.gym_wrapper import CARLAEnv
from utils.pre_process import generate_costmap, get_costmap_stack
from utils import GlobalDict
import carla_utils as cu

from rl import TD3

import os
import cv2
import time
import copy
import threading
import random
import argparse
import numpy as np
from PIL import Image
from datetime import datetime

import torch
from torch.autograd import grad
import torchvision.transforms as transforms

import gym

global_dict = GlobalDict()

global_dict['img'] = None
global_dict['view_img'] = None
global_dict['pcd'] = None
global_dict['nav'] = None
global_dict['cost_map'] = None

global_dict['vel'] = 0.
global_dict['a'] = 0.
global_dict['cnt'] = 0
global_dict['v0'] = 0.
global_dict['plan_time'] = 0.
global_dict['trajectory'] = None
global_dict['vehicle'] = None
global_dict['plan_map'] = None
global_dict['transform'] = None
global_dict['draw_cost_map'] = None
global_dict['max_steer_angle'] = 0.
global_dict['ipm_image'] = np.zeros((200,400), dtype=np.uint8)
global_dict['ipm_image'].fill(255)
global_dict['trans_costmap_dict'] = {}
global_dict['state0'] = None
global_dict['start_control'] = False

random.seed(datetime.now())
torch.manual_seed(999)
torch.cuda.manual_seed(999)
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

generator = GeneratorUNet()
generator = generator.to(device)
generator.load_state_dict(torch.load('../ckpt/g.pth'))
trajectory_model = Generator(4).to(device)
trajectory_model.load_state_dict(torch.load('../ckpt/il/generator.pth'))
trajectory_model.eval()
generator.eval()
#model.eval()

parser = argparse.ArgumentParser(description='Params')
parser.add_argument('--name', type=str, default="rl-test-03", help='name of the script')
parser.add_argument('-d', '--data', type=int, default=1, help='data index')
parser.add_argument('-s', '--save', type=bool, default=False, help='save result')
parser.add_argument('--width', type=int, default=400, help='image width')
parser.add_argument('--height', type=int, default=200, help='image height')
parser.add_argument('--trans_width', type=int, default=256, help='transform image width')
parser.add_argument('--trans_height', type=int, default=128, help='transform image height')
parser.add_argument('--vector_dim', type=int, default=2, help='vector dim')
parser.add_argument('--max_dist', type=float, default=25., help='max distance')
parser.add_argument('--max_t', type=float, default=5., help='max time')
parser.add_argument('--scale', type=float, default=25., help='longitudinal length')
parser.add_argument('--dt', type=float, default=0.05, help='discretization minimum time interval')
parser.add_argument('--max_speed', type=float, default=10., help='max speed')
parser.add_argument('--rnn_steps', type=int, default=2, help='rnn readout steps')

args = parser.parse_args()

data_index = args.data
save_path = '/media/wang/DATASET/CARLA/town01/'+str(data_index)+'/'

img_transforms = [
    transforms.Resize((args.trans_height, args.trans_width)),
    transforms.ToTensor(),
    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
]
img_trans = transforms.Compose(img_transforms)

cost_map_transforms_ = [ transforms.Resize((200, 400), Image.BICUBIC),
    transforms.ToTensor(),
    transforms.Normalize((0.5), (0.5))
    ]
cost_map_trans = transforms.Compose(cost_map_transforms_)
        
class Param(object):
    def __init__(self):
        self.longitudinal_length = args.scale
        self.ksize = 21
        
param = Param()
sensor = Sensor(sensor_dict['camera']['transform'], config['camera'])
sensor_master = CarlaSensorMaster(sensor, sensor_dict['camera']['transform'], binded=True)
inverse_perspective_mapping = InversePerspectiveMapping(param, sensor_master)

def mkdir(path):
    os.makedirs(save_path+path, exist_ok=True)

def image_callback(data):
    global_dict['plan_time'] = time.time()
    array = np.frombuffer(data.raw_data, dtype=np.dtype("uint8")) 
    array = np.reshape(array, (data.height, data.width, 4)) # RGBA format
    global_dict['img'] = array
    global_dict['transform'] = global_dict['vehicle'].get_transform()
    try:
        global_dict['nav'] = get_nav(global_dict['vehicle'], global_dict['plan_map'])
        v = global_dict['vehicle'].get_velocity()
        global_dict['v0'] = np.sqrt(v.x**2+v.y**2+v.z**2)
    except:
        pass
    
def view_image_callback(data):
    array = np.frombuffer(data.raw_data, dtype=np.dtype("uint8")) 
    array = np.reshape(array, (data.height, data.width, 4)) # RGBA format
    global_dict['view_img'] = array
    
def lidar_callback(data):
    ts = time.time()
    lidar_data = np.frombuffer(data.raw_data, dtype=np.float32).reshape([-1, 3])
    point_cloud = np.stack([-lidar_data[:,1], -lidar_data[:,0], -lidar_data[:,2]])
    mask = np.where(point_cloud[2] > -2.3)[0]
    point_cloud = point_cloud[:, mask]
    global_dict['pcd'] = point_cloud
    generate_costmap(ts, global_dict, cost_map_trans, args)

def collision_callback(data):
    global_dict['collision'] = True
    #print(data)
    
def get_cGAN_result(img, nav):
    img = Image.fromarray(cv2.cvtColor(img,cv2.COLOR_BGR2RGB))
    nav = Image.fromarray(cv2.cvtColor(nav,cv2.COLOR_BGR2RGB))
    img = img_trans(img)
    nav = img_trans(nav)
    input_img = torch.cat((img, nav), 0).unsqueeze(0).to(device)
    result = generator(input_img)
    result = result.cpu().data.numpy()[0][0]*127+128
    result = cv2.resize(result, (global_dict['img'].shape[1], global_dict['img'].shape[0]), interpolation=cv2.INTER_CUBIC)
    return result

def make_plan():
    while True:
        try:
            result = get_cGAN_result(global_dict['img'], global_dict['nav'])
            # 2. inverse perspective mapping and get costmap
            img = copy.deepcopy(global_dict['img'])
            mask = np.where(result > 200)
            img[mask[0],mask[1]] = (255, 0, 0, 255)
            
            ipm_image = inverse_perspective_mapping.getIPM(result)
            global_dict['ipm_image'] = ipm_image
        except:
            pass
    
def main():
    client = carla.Client(config['host'], config['port'])
    client.set_timeout(config['timeout'])
    
    world = client.load_world('Town01')

    weather = carla.WeatherParameters(
        cloudiness=random.randint(0,10),
        precipitation=0,
        sun_altitude_angle=random.randint(50,90)
    )
    
    set_weather(world, weather)
    
    blueprint = world.get_blueprint_library()
    world_map = world.get_map()
    
    vehicle = add_vehicle(world, blueprint, vehicle_type='vehicle.audi.a2')
    #vehicle = add_vehicle(world, blueprint, vehicle_type='vehicle.yamaha.yzf')
    #vehicle = add_vehicle(world, blueprint, vehicle_type='vehicle.*.*')
    global_dict['vehicle'] = vehicle
    # Enables or disables the simulation of physics on this actor.
    vehicle.set_simulate_physics(True)
    physics_control = vehicle.get_physics_control()
    global_dict['max_steer_angle'] = np.deg2rad(physics_control.wheels[0].max_steer_angle)
    sensor_dict = {
        'camera':{
            'transform':carla.Transform(carla.Location(x=0.5, y=0.0, z=2.5)),
            'callback':image_callback,
            },
        'camera:view':{
            #'transform':carla.Transform(carla.Location(x=0.0, y=4.0, z=4.0), carla.Rotation(pitch=-30, yaw=-60)),
            'transform':carla.Transform(carla.Location(x=-3.0, y=0.0, z=9.0), carla.Rotation(pitch=-45)),
            #'transform':carla.Transform(carla.Location(x=0.0, y=0.0, z=6.0), carla.Rotation(pitch=-90)),
            'callback':view_image_callback,
            },
        'lidar':{
            'transform':carla.Transform(carla.Location(x=0.5, y=0.0, z=2.5)),
            'callback':lidar_callback,
            },
        'collision':{
            'transform':carla.Transform(carla.Location(x=0.5, y=0.0, z=2.5)),
            'callback':collision_callback,
            },
        }

    sm = SensorManager(world, blueprint, vehicle, sensor_dict)
    sm.init_all()
    
    model = TD3(buffer_size=3e3, noise_decay_steps=5e3, batch_size=10)
    
    env = CARLAEnv(world, vehicle, global_dict, args, trajectory_model)
    state = env.reset()
    
    plan_thread = threading.Thread(target = make_plan, args=())
    
    while True:
        if (global_dict['img'] is not None) and (global_dict['nav'] is not None) and (global_dict['pcd'] is not None):
            plan_thread.start()
            break
        else:
            time.sleep(0.001)

    # start to control
    print('Start to control')

    episode_timesteps = 0
    episode_reward = 0
    max_steps = 1e9
    total_steps = 0
    max_episode_steps = 1000
    learning_starts = 50
    episode_num = 0
    
    while total_steps < max_steps:
        total_steps += 1
        episode_timesteps = 0
        episode_reward = 0
        
        state_queue = []
        state = env.reset()
        for _ in range(10): state_queue.append(state)
        state = torch.stack(state_queue)
        plan_time = global_dict['plan_time']
        
        for step in range(max_episode_steps):
            global_dict['state0'] = cu.getActorState('odom', plan_time, global_dict['vehicle'])
            global_dict['state0'].x = global_dict['transform'].location.x
            global_dict['state0'].y = global_dict['transform'].location.y
            global_dict['state0'].z = global_dict['transform'].location.z
            global_dict['state0'].theta = np.deg2rad(global_dict['transform'].rotation.yaw)
    
            action = model.policy_net.get_action(state)
            print('org:', action)
            action = model.noise.get_action(action, total_steps)
            print('after', action)
            next_state, reward, done, _ = env.step(action, plan_time)
            plan_time = global_dict['plan_time']
            model.replay_buffer.push(copy.deepcopy(state), action, reward, copy.deepcopy(next_state), done)

            state = next_state
            episode_reward += reward
            total_steps += 1
            episode_timesteps += 1
            
            if done or episode_timesteps == max_episode_steps:
                if len(model.replay_buffer) > learning_starts:
                    for i in range(episode_timesteps):
                        model.train_step(i)

                episode_num += 1
                if episode_num > 0 and episode_num % model.save_eps_num == 0:
                    model.save(directory=model.load_dir, filename=str(episode_num))
                    
                model.writer.add_scalar('episode_reward', episode_reward, episode_num)    
                break

    sm.close_all()
    vehicle.destroy()
        
if __name__ == '__main__':
    main()
