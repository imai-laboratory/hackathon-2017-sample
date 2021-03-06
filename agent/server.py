# -*- coding: utf-8 -*-
import argparse
import io
import os
from threading import Lock

import cherrypy
from wsgiref.simple_server import make_server, WSGIServer
from SocketServer import ThreadingMixIn

import msgpack
import numpy as np
from PIL import Image
from PIL import ImageOps

from config import BRICA_CONFIG_FILE
from config.model import TF_CNN_FEATURE_EXTRACTOR

from tfalex.FeatureExtractor import FeatureExtractor
from tool.visualizer import AnimatedLineGraph

import logging
import logging.config
from config.log import CHERRYPY_ACCESS_LOG, CHERRYPY_ERROR_LOG, LOGGING, APP_KEY, INBOUND_KEY, OUTBOUND_KEY
from cognitive.service import AgentService
from tool.result_logger import ResultLogger

import tensorflow as tf
from ml.agent import Agent
from ml.network import make_network
from ml.dnd import DND
from lightsaber.tensorflow.util import initialize

logging.config.dictConfig(LOGGING)

inbound_logger = logging.getLogger(INBOUND_KEY)
app_logger = logging.getLogger(APP_KEY)
outbound_logger = logging.getLogger(OUTBOUND_KEY)


class ThreadingWsgiServer(ThreadingMixIn, WSGIServer):
    pass


def unpack(payload, depth_image_count=1, depth_image_dim=32*32):
    dat = msgpack.unpackb(payload)

    image = []
    for i in xrange(depth_image_count):
        image.append(Image.open(io.BytesIO(bytearray(dat['image'][i]))))

    depth = []
    for i in xrange(depth_image_count):
        d = (Image.open(io.BytesIO(bytearray(dat['depth'][i]))))
        depth.append(np.array(ImageOps.grayscale(d)).reshape(depth_image_dim))

    reward = dat['reward']
    observation = {"image": image, "depth": depth}
    rotation = dat['rotation']
    movement = dat['movement']
    scene_num = dat['scene_num']

    return reward, observation, rotation, movement, scene_num


def unpack_reset(payload):
    dat = msgpack.unpackb(payload)
    reward = dat['reward']
    success = dat['success']
    failure = dat['failure']
    elapsed = dat['elapsed']
    finished = dat['finished']

    return reward, success, failure, elapsed, finished


use_gpu = int(os.getenv('GPU', '-1'))
depth_image_dim = 32 * 32
depth_image_count = 1
image_feature_dim = 256 * 6 * 6
image_feature_count = 1
feature_output_dim = (depth_image_dim * depth_image_count) + (image_feature_dim * image_feature_count)


class Root(object):
    def __init__(self, sess, logdir, num_workers, visualize):
        self.latest_stage = -1
        self.sess = sess
        with sess.as_default():
            model = make_network()
            dnds = []
            for i in range(3):
                dnds.append(DND())
            global_agent = Agent(model, dnds, 3, name='global')

            self.agents = []
            self.popped_agents = {}
            self.popped_locks = {}

            # CREATE NEW AGENT(S)
            for i in range(num_workers):
                # CREATE PLOTTER PER AGENT
                plotter = (AnimatedLineGraph(0, 0, max_val=50)
                           if visualize else None)

                self.agents.append(Agent(model, dnds, 3,
                                         name='worker{}'.format(i),
                                         plotter=plotter)
                                   )
            summary_writer = tf.summary.FileWriter(logdir, sess.graph)
            for agent in self.agents:
                agent.set_summary_writer(summary_writer)
            initialize()

            # load feature extractor (alex net)
            if os.path.exists(TF_CNN_FEATURE_EXTRACTOR):
                config = tf.ConfigProto(gpu_options=tf.GPUOptions(visible_device_list='0', allow_growth=True))
                gpu_config =  config # TODO: remove this
                app_logger.info("loading... {}".format(TF_CNN_FEATURE_EXTRACTOR))
                self.feature_extractor = FeatureExtractor(sess_name='AlexNet',
                                                          sess_config=gpu_config)
                app_logger.info("done")

            else:
                raise Exception

            self.agent_service = AgentService(BRICA_CONFIG_FILE, self.feature_extractor, sess)
            self.result_logger = ResultLogger()

    @cherrypy.expose()
    def flush(self, identifier):
        if identifier not in self.popped_agents:
            if len(self.agents) > 0:
                self.popped_locks[identifier] = Lock()
                agent = self.agents.pop(0)
                self.popped_agents[identifier] = agent
                self.agent_service.initialize(identifier, agent)
            else:
                return
        else:
            agent = self.popped_agents[identifier]
        self.popped_locks[identifier].acquire()
        with self.sess.as_default():
            self.agent_service.initialize(identifier, agent)
        self.popped_locks[identifier].release()

    @cherrypy.expose
    def create(self, identifier):
        if identifier not in self.popped_agents:
            if __debug__:
                os.system('spd-say "Agent Created"')
            if len(self.agents) > 0:
                self.popped_locks[identifier] = Lock()
                agent = self.agents.pop(0)
                self.popped_agents[identifier] = agent
            else:
                return
        else:
            agent = self.popped_agents[identifier]
        self.popped_locks[identifier].acquire()
        with self.sess.as_default():
            body = cherrypy.request.body.read()
            reward, observation, rotation, movement, scene_num = unpack(body)
            self.latest_stage = max(scene_num, self.latest_stage)

            inbound_logger.info('id: {}, reward: {}, depth: {}'.format(
                identifier, reward, observation['depth']
            ))
            feature = self.feature_extractor.feature(observation)
            self.result_logger.initialize()
            result = self.agent_service.create(reward, feature, identifier, agent)

            outbound_logger.info('id:{}, action: {}'.format(identifier, result))
        self.popped_locks[identifier].release()
        return str(result)

    @cherrypy.expose
    def step(self, identifier):
        if identifier in self.popped_locks:
            self.popped_locks[identifier].acquire()
        with self.sess.as_default():
            body = cherrypy.request.body.read()
            reward, observation, rotation, movement, scene_num = unpack(body)
            self.latest_stage = max(scene_num, self.latest_stage)

            inbound_logger.info('id: {}, reward: {}, depth: {}'.format(
                identifier, reward, observation['depth']
            ))

            result = self.agent_service.step(reward, rotation, movement, observation, identifier)
            self.result_logger.step()
            outbound_logger.info('id: {}, result: {}'.format(
                identifier, result
            ))
        if identifier in self.popped_locks:
            self.popped_locks[identifier].release()
        return str(result) + "/" + str(self.latest_stage)

    @cherrypy.expose
    def reset(self, identifier):
        if identifier in self.popped_locks:
            self.popped_locks[identifier].acquire()
        with self.sess.as_default():
            body = cherrypy.request.body.read()
            reward, success, failure, elapsed, finished = unpack_reset(body)

            inbound_logger.info('reward: {}, success: {}, failure: {}, elapsed: {}'.format(
                reward, success, failure, elapsed))

            result = self.agent_service.reset(reward, identifier)
            self.result_logger.report(success, failure, finished)

            outbound_logger.info('result: {}'.format(result))
        if identifier in self.popped_locks:
            self.popped_locks[identifier].release()
        return str(result)

def main(args):
    config = tf.ConfigProto(gpu_options=tf.GPUOptions(visible_device_list=args.gpu, allow_growth=True))
    sess = tf.Session(config=config)
    cherrypy.config.update({'server.socket_host': args.host, 'server.socket_port': args.port, 'log.screen': False,
                            'log.access_file': CHERRYPY_ACCESS_LOG, 'log.error_file': CHERRYPY_ERROR_LOG})

    # Cherrypy Original
    # cherrypy.quickstart(Root(sess, args.logdir, args.workers))

    # GEvent
    # app = cherrypy.tree.mount(Root(sess, args.logdir, args.workers), '/')
    # wsgi.WSGIServer((args.host, args.port), app).serve_forever()

    # wsgiserver
    # app = cherrypy.tree.mount(Root(sess, args.logdir, args.workers), '/')
    # server = wsgiserver.WSGIServer(app, host=args.host, port=args.port)
    # server.start()

    # wsgiref
    app = cherrypy.tree.mount(Root(sess, args.logdir, args.workers,
                                   args.visualize), '/')
    server = make_server(args.host, args.port, app, ThreadingWsgiServer)
    server.serve_forever()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='LIS Backend')
    parser.add_argument('--host', default='localhost', type=str, help='Server hostname')
    parser.add_argument('--port', default=8765, type=int, help='Server port number')
    parser.add_argument('--gpu', default='-1', type=str, help='Gpu id')
    parser.add_argument('--logdir', default='board', type=str, help='log directory for tensorboard')
    parser.add_argument('--workers', default=4, type=int, help='the number of workers')
    parser.add_argument('--visualize', action='store_true')
    args = parser.parse_args()

    main(args)
