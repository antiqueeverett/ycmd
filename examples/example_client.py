#!/usr/bin/env python
#
# Copyright (C) 2014  Google Inc.
#
# This file is part of YouCompleteMe.
#
# YouCompleteMe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# YouCompleteMe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with YouCompleteMe.  If not, see <http://www.gnu.org/licenses/>.

from base64 import b64encode, b64decode
import collections
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import urlparse
import time

import requests
# enum34 on PyPi
from enum import Enum

HEADERS = {'content-type': 'application/json'}
HMAC_HEADER = 'x-ycm-hmac'
HMAC_SECRET_LENGTH = 16
SERVER_IDLE_SUICIDE_SECONDS = 10800  # 3 hours
MAX_SERVER_WAIT_TIME_SECONDS = 5

# Set this to True to see ycmd's output interleaved with the client's
INCLUDE_YCMD_OUTPUT = False
CODE_COMPLETIONS_HANDLER = '/completions'
EVENT_HANDLER = '/event_notification'
EXTRA_CONF_HANDLER = '/load_extra_conf_file'
DIR_OF_THIS_SCRIPT = os.path.dirname( os.path.abspath( __file__ ) )
PATH_TO_YCMD = os.path.join( DIR_OF_THIS_SCRIPT, '..', 'ycmd' )
PATH_TO_EXTRA_CONF = os.path.join( DIR_OF_THIS_SCRIPT, '.ycm_extra_conf.py' )


class Event( Enum ):
  FileReadyToParse = 1
  BufferUnload = 2
  BufferVisit = 3
  InsertLeave = 4
  CurrentIdentifierFinished = 5


# Wrapper around ycmd's HTTP+JSON API
class YcmdHandle( object ):
  def __init__( self, popen_handle, port, hmac_secret ):
    self._popen_handle = popen_handle
    self._port = port
    self._hmac_secret = hmac_secret
    self._server_location = 'http://127.0.0.1:' + str( port )


  @classmethod
  def StartYcmdAndReturnHandle( cls ):
    prepared_options = DefaultSettings()
    hmac_secret = os.urandom( HMAC_SECRET_LENGTH )
    prepared_options[ 'hmac_secret' ] = b64encode( hmac_secret )

    # The temp options file is deleted by ycmd during startup
    with tempfile.NamedTemporaryFile( delete = False ) as options_file:
      json.dump( prepared_options, options_file )
      options_file.flush()
      server_port = GetUnusedLocalhostPort()
      ycmd_args = [ sys.executable,
                    PATH_TO_YCMD,
                    '--port={0}'.format( server_port ),
                    '--options_file={0}'.format( options_file.name ),
                    '--idle_suicide_seconds={0}'.format(
                      SERVER_IDLE_SUICIDE_SECONDS ) ]

      std_handles = None if INCLUDE_YCMD_OUTPUT else subprocess.PIPE
      child_handle = subprocess.Popen( ycmd_args,
                                       stdout = std_handles,
                                       stderr = std_handles )
      return cls( child_handle, server_port, hmac_secret )


  def IsAlive( self ):
    returncode = self._popen_handle.poll()
    # When the process hasn't finished yet, poll() returns None.
    return returncode is None


  def IsHealthy( self ):
    if not self.IsAlive():
      return False
    response = self.GetFromHandlerAndLog( 'healthy' )
    response.raise_for_status()
    return response.json()


  def Shutdown( self ):
    if self.IsAlive():
      self._popen_handle.terminate()


  def PostToHandlerAndLog( self, data, handler ):
    sent_data = ToUtf8Json( data )
    response = requests.post( self._BuildUri( handler ),
                              data = sent_data,
                              headers = self._ExtraHeaders( sent_data ) )
    LogRequestAndResponse( response )
    self._ValidateResponseObject( response )
    return response


  def GetFromHandlerAndLog( self, handler ):
    response = requests.get( self._BuildUri( handler ),
                             headers = self._ExtraHeaders() )
    LogRequestAndResponse( response )
    self._ValidateResponseObject( response )
    return response


  def SendCodeCompletionRequest( self,
                                 test_filename,
                                 filetype,
                                 line_num,
                                 column_num ):
    request_json = BuildRequestData( test_filename = test_filename,
                                     filetype = filetype,
                                     line_num = line_num,
                                     column_num = column_num )
    response_json = self.PostToHandlerAndLog( request_json,
                                              CODE_COMPLETIONS_HANDLER )


  def SendEventNotification( self,
                             event_enum,
                             test_filename,
                             filetype,
                             line_num = 1,  # just placeholder values
                             column_num = 1,
                             extra_data = None ):
    request_json = BuildRequestData( test_filename = test_filename,
                                     filetype = filetype,
                                     line_num = line_num,
                                     column_num = column_num )
    if extra_data:
      request_json.update( extra_data )
    request_json[ 'event_name' ] = event_enum.name
    response_json = self.PostToHandlerAndLog( request_json,
                                              EVENT_HANDLER )


  def LoadExtraConfFile( self, extra_conf_filename ):
    request_json = { 'filepath': extra_conf_filename }
    response_json = self.PostToHandlerAndLog( request_json, EXTRA_CONF_HANDLER )


  def WaitUntilReady( self ):
    total_slept = 0
    time.sleep( 0.5 )
    total_slept += 0.5
    while True:
      try:
        if total_slept > MAX_SERVER_WAIT_TIME_SECONDS:
          raise RuntimeError(
              'ycmd hasn\'t started after {0} seconds, aborting'.format(
                    MAX_SERVER_WAIT_TIME_SECONDS ) )

        if self.IsHealthy():
          return
      except requests.exceptions.ConnectionError:
        time.sleep( 0.1 )
        total_slept += 0.1


  def _ExtraHeaders( self, request_body = None ):
    if not request_body:
      request_body = ''
    headers = dict( HEADERS )
    headers[ HMAC_HEADER ] = b64encode(
        CreateHexHmac( request_body, self._hmac_secret ) )
    return headers


  def _BuildUri( self, handler ):
    return urlparse.urljoin( self._server_location, handler )


  def _ValidateResponseObject( self, response ):
    if not ContentHexHmacValid(
        response.content,
        b64decode( response.headers[ HMAC_HEADER ] ),
        self._hmac_secret ):
      raise RuntimeError( 'Received invalid HMAC for response!' )
    return True


def LogRequestAndResponse( response ):
  def RequestAsString( request ):
    headers = '\n'.join( '{}: {}'.format( k, v )
                         for k, v in request.headers.items() )

    return '{} {}\n{}\n\n{}'.format( request.method,
                                    request.url,
                                    headers,
                                    request.body )

  def ResponseAsString( response ):
    return response.text

  print '====== Request ====='
  print RequestAsString( response.request )
  print '====== Response ====='
  print ResponseAsString( response )
  print '\n'


def ContentHexHmacValid( content, hmac, hmac_secret ):
  return SecureCompareStrings( CreateHexHmac( content, hmac_secret ), hmac )


def CreateHexHmac( content, hmac_secret ):
  # Must ensure that hmac_secret is str and not unicode
  return hmac.new( str( hmac_secret ),
                   msg = content,
                   digestmod = hashlib.sha256 ).hexdigest()


# This is the compare_digest function from python 3.4, adapted for 2.7:
#   http://hg.python.org/cpython/file/460407f35aa9/Lib/hmac.py#l16
def SecureCompareStrings( a, b ):
  """Returns the equivalent of 'a == b', but avoids content based short
  circuiting to reduce the vulnerability to timing attacks."""
  if not ( isinstance( a, str ) and isinstance( b, str ) ):
    raise TypeError( "inputs must be str instances" )

  # The length of the expected digest is public knowledge.
  if len( a ) != len( b ):
    return False

  # We assume that integers in the bytes range are all cached,
  # thus timing shouldn't vary much due to integer object creation
  result = 0
  for x, y in zip( a, b ):
    result |= ord( x ) ^ ord( y )
  return result == 0


# Recurses through the object if it's a dict/iterable and converts all the
# unicode objects to utf-8 strings.
def RecursiveEncodeUnicodeToUtf8( value ):
  if isinstance( value, unicode ):
    return value.encode( 'utf8' )
  if isinstance( value, str ):
    return value
  elif isinstance( value, collections.Mapping ):
    return dict( map( RecursiveEncodeUnicodeToUtf8, value.iteritems() ) )
  elif isinstance( value, collections.Iterable ):
    return type( value )( map( RecursiveEncodeUnicodeToUtf8, value ) )
  else:
    return value


def ToUtf8Json( data ):
  return json.dumps( RecursiveEncodeUnicodeToUtf8( data ),
                     ensure_ascii = False,
                     # This is the encoding of INPUT str data
                     encoding = 'utf-8' )


def PathToTestFile( filename ):
  return os.path.join( DIR_OF_THIS_SCRIPT, filename )


def DefaultSettings():
  default_options_path = os.path.join( DIR_OF_THIS_SCRIPT,
                                       '..',
                                      'ycmd',
                                      'default_settings.json' )

  with open( default_options_path ) as f:
    return json.loads( f.read() )


def GetUnusedLocalhostPort():
  sock = socket.socket()
  # This tells the OS to give us any free port in the range [1024 - 65535]
  sock.bind( ( '', 0 ) )
  port = sock.getsockname()[ 1 ]
  sock.close()
  return port


def PrettyPrintDict( value ):
  # Sad that this works better than pprint...
  return json.dumps( value, sort_keys = True, indent = 2 ).replace(
        '\\n', '\n')


def BuildRequestData( test_filename = None,
                      filetype = None,
                      line_num = None,
                      column_num = None ):
  test_path = PathToTestFile( test_filename )

  # Normally, this would be the contents of the file as loaded in the editor
  # (possibly unsaved data).
  contents = open( test_path ).read()

  return {
    'line_num': line_num,
    'column_num': column_num,
    'filepath': test_path,
    'file_data': {
      test_path: {
        'filetypes': [ filetype ],
        'contents': contents
      }
    }
  }


def PythonSemanticCompletionResults( server ):
  server.SendEventNotification( Event.FileReadyToParse,
                                test_filename = 'some_python.py',
                                filetype = 'python' )

  server.SendCodeCompletionRequest( test_filename = 'some_python.py',
                                    filetype = 'python',
                                    line_num = 30,
                                    column_num = 6 )


def LanguageAgnosticIdentifierCompletion( server ):
  # We're using JavaScript here, but the language doesn't matter; the identifier
  # completion engine just extracts identifiers.
  server.SendEventNotification( Event.FileReadyToParse,
                                test_filename = 'some_javascript.js',
                                filetype = 'javascript' )

  server.SendCodeCompletionRequest( test_filename = 'some_javascript.js',
                                    filetype = 'javascript',
                                    line_num = 24,
                                    column_num = 6 )


def CppSemanticCompletionResults( server ):
  # TODO: document this better
  server.LoadExtraConfFile( PATH_TO_EXTRA_CONF )

  server.SendEventNotification( Event.FileReadyToParse,
                                test_filename = 'some_cpp.cpp',
                                filetype = 'cpp' )

  server.SendCodeCompletionRequest( test_filename = 'some_cpp.cpp',
                                    filetype = 'cpp',
                                    line_num = 28,
                                    column_num = 7 )


def Main():
  print 'Trying to start server...'
  server = YcmdHandle.StartYcmdAndReturnHandle()
  server.WaitUntilReady()
  LanguageAgnosticIdentifierCompletion( server )
  PythonSemanticCompletionResults( server )
  CppSemanticCompletionResults( server )
  server.Shutdown()


if __name__ == "__main__":
  Main()