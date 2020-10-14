#!/usr/bin/env python

import wave
import datetime
import logging
import os
from os import path
from logging import debug, info
import tornado.ioloop
import tornado.websocket
import tornado.httpserver
import tornado.template
import tornado.web
import webrtcvad
from tornado.web import url
import json
import re
import speech_recognition as sr


logging.captureWarnings(True)

# Constants:
MS_PER_FRAME = int(os.environ.get('MS_PER_FRAME',20))  # Duration of a frame in ms
DEFAULT_SAMPLE_RATE = os.environ.get('DEFAULT_SAMPLE_RATE',16000)
SILENCE = int(os.environ.get('SILENCE', 10)) # How many continuous frames of silence determine the end of a phrase
CLIP_MIN_MS = int(os.environ.get('CLIP_MIN_MS', 200))  # ms - the minimum audio clip that will be used
MAX_LENGTH = os.environ.get('MAX_LENGTH',3000)  # Max length of a sound clip for processing in ms
VAD_SENSITIVITY = os.environ.get('VAD_SENSITIVITY',3)
CLIP_MIN_FRAMES = CLIP_MIN_MS // MS_PER_FRAME
NEXMO_PHONE_NUMBER = 12016728675

# Global variables
conns = {}
model = None
DEBUG = True

class BufferedPipe(object):
	def __init__(self, max_frames, sink):
		"""
		Create a buffer which will call the provided `sink` when full.

		It will call `sink` with the number of frames and the accumulated bytes when it reaches
		`max_buffer_size` frames.
		"""
		self.sink = sink
		self.max_frames = max_frames

		self.count = 0
		self.payload = b''

	def append(self, data, id):
		""" Add another data to the buffer. `data` should be a `bytes` object. """

		self.count += 1
		self.payload += data

		if self.count == self.max_frames:
			self.process(id)

	def process(self, id):
		""" Process and clear the buffer. """
		self.sink(self.count, self.payload, id)
		self.count = 0
		self.payload = b''

class SpeechTranslate():
	def __init__(self):
		self.r = sr.Recognizer()

	def process(self, path):
		try:
			audio_file = sr.AudioFile(path)
			with audio_file as source:
				audio = self.r.record(source)	
			result = self.r.recognize_google(audio)

			return result
		except Exception as ee:
			return None

class MLModel(object):
		def __init__(self):
			info("loading fastai model..")
			
		def predict_from_file(self, wav_file, verbose=True):
			print(wav_file)
			
class AudioProcessor(object):
	def __init__(self, path, uuid, mode, sample_rate=DEFAULT_SAMPLE_RATE):
		self._path = path
		self.uuid = uuid
		self.mode = mode
		self.sample_rate = sample_rate
		self.speech_r = SpeechTranslate()

	def process(self, count, payload, uuid):
		if count > CLIP_MIN_FRAMES :  # If the buffer is less than CLIP_MIN_MS, ignore it
			fn = "rec-{}.wav".format(datetime.datetime.now().strftime("%Y%m%dT%H%M%S,%f"))
			info("recording clip {}".format(fn))

			output = wave.open(fn, 'wb')
			output.setparams(
				(1, 2, self.sample_rate, 0, 'NONE', 'not compressed'))
			output.writeframes(payload)
			output.close()

			text = self.speech_r.process(fn)
			print(text)
			os.remove(fn)
			# prediction, label = model.predict_from_file(fn)
		else:
			info('Discarding {} frames'.format(str(count)))


class WSHandler(tornado.websocket.WebSocketHandler):
	def check_origin(self, origin):
		return True

	def initialize(self):
		# Create a buffer which will call `process` when it is full:
		self.frame_buffer = None
		self.call_frame_buffer = b'' # used to record call as 1 recording
		# Setup the Voice Activity Detector
		self.tick = None
		self.id = None#uuid.uuid4().hex
		self.vad = webrtcvad.Vad()
		self.sample_rate = DEFAULT_SAMPLE_RATE #default sample rate
		# Level of sensitivity

		self.vad.set_mode(VAD_SENSITIVITY)

		self.processor = None
		self.path = None

	def open(self, path):
		info("client connected")
		debug(self.request.uri)
		self.path = self.request.uri
		self.tick = 0

	def on_message(self, message):
		# Check if message is Binary or Text

		if type(message) != str:
			self.call_frame_buffer += message
			if self.vad.is_speech(message,self.sample_rate):
				debug("SPEECH from {}".format(self.id))
				self.tick = SILENCE
				self.frame_buffer.append(message, self.id)
			else:
				debug("Silence from {} TICK: {}".format(self.id, self.tick))
				self.tick -= 1
				if self.tick == 0:
					# Force processing and clearing of the buffer
					self.frame_buffer.process(self.id)
		else:
			info(message)
			# Here we should be extracting the meta data that was sent and attaching it to the connection object
			data = json.loads(message)

			info('>>> WSHandler on_message data: {}'.format(data))
			if data.get('content-type'):

				original_call_uuid = ""
				content_type = data.get('content-type')

				self.sample_rate = int(re.findall("\+?\d+$",content_type)[0])
				mode = data["mode"] if "mode" in data else "EVAL"

				self.id = original_call_uuid

				conns[self.id] = self

				self.processor = AudioProcessor(
					self.path, original_call_uuid, mode, self.sample_rate).process
				self.frame_buffer = BufferedPipe(MAX_LENGTH // MS_PER_FRAME, self.processor)
				self.write_message('ok')

	def on_close(self):
		# Remove the connection from the list of connections
		del conns[self.id]
		info("client disconnected {} ".format(self.id))


# Websocket events
class WsEventHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def post(self):
		info("/event {}".format(self.request.body))
		self.content_type = 'text/plain'
		self.write('ok')
		self.finish()


class AcceptNumberHandler(tornado.web.RequestHandler):
	@tornado.web.asynchronous
	def get(self):
		ncco = [
			  {
				"action": "talk",
				"text": "Thanks. Connecting you now"
			  },
			 
			  {
				 "action": "connect",
				 "from": NEXMO_PHONE_NUMBER,
				 "endpoint": [
					 {
						"type": "websocket",
						"uri" : "ws://"+ self.request.host +"/socket",
						"content-type": "audio/l16;rate=16000"
					 }
				 ]
			   }
			]
		
		# for file in glob.glob("static/*"):
		# 	ncco.append({"action": "stream","streamUrl": ['https://' + self.request.host +'/'+file]})

		print(json.dumps(ncco))
		self.write(json.dumps(ncco))
		self.set_header("Content-Type", 'application/json; charset="utf-8"')
		self.finish()

def main():
	try:
		logging.getLogger().setLevel(logging.INFO)
		logging.getLogger('tornado.access').disabled = True
		global model
		model = MLModel()

		application = tornado.web.Application([
			(r"/ws_event", WsEventHandler),
			(r"/answer", AcceptNumberHandler),
			(r'/static/(.*)', tornado.web.StaticFileHandler, {'path': 'static'}),
			url(r"/socket(.*)", WSHandler),
		])
		http_server = tornado.httpserver.HTTPServer(application)
		port = int(os.getenv('PORT', 8000))
		info("running on port  {}".format(port))

		http_server.listen(port)
		tornado.ioloop.IOLoop.instance().start()
	except KeyboardInterrupt:
		pass  # Suppress the stack-trace on quit


if __name__ == "__main__":
	main()
