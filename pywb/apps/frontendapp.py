from gevent.monkey import patch_all; patch_all()

#from bottle import run, Bottle, request, response, debug
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.wsgi import pop_path_info
from six.moves.urllib.parse import urljoin
from six import iteritems
from warcio.statusandheaders import StatusAndHeaders
from warcio.utils import to_native_str
from warcio.timeutils import iso_date_to_timestamp
from wsgiprox.wsgiprox import WSGIProxMiddleware

from pywb.recorder.multifilewarcwriter import MultiFileWARCWriter
from pywb.recorder.recorderapp import RecorderApp

from pywb.utils.loaders import load_yaml_config
from pywb.utils.geventserver import GeventServer
from pywb.utils.io import StreamIter

from pywb.warcserver.warcserver import WarcServer

from pywb.rewrite.templateview import BaseInsertView

from pywb.apps.static_handler import StaticHandler
from pywb.apps.rewriterapp import RewriterApp, UpstreamException
from pywb.apps.wbrequestresponse import WbResponse

import os
import re

import traceback
import requests
import logging


# ============================================================================
class FrontEndApp(object):
    """Orchestrates pywb's core Wayback Machine functionality and is comprised of 2 core sub-apps and 3 optional apps.

    Sub-apps:
      - WarcServer: Serves the archive content (WARC/ARC and index) as well as from the live web in record/proxy mode
      - RewriterApp: Rewrites the content served by pywb (if it is to be rewritten)
      - WSGIProxMiddleware (Optional): If proxy mode is enabled, performs pywb's HTTP(s) proxy functionality
      - AutoIndexer (Optional): If auto-indexing is enabled for the collections it is started here
      - RecorderApp (Optional): Recording functionality, available when recording mode is enabled
    """

    REPLAY_API = 'http://localhost:%s/{coll}/resource/postreq'
    CDX_API = 'http://localhost:%s/{coll}/index'
    RECORD_SERVER = 'http://localhost:%s'
    RECORD_API = 'http://localhost:%s/%s/resource/postreq?param.recorder.coll={coll}'

    RECORD_ROUTE = '/record'

    PROXY_CA_NAME = 'pywb HTTPS Proxy CA'

    PROXY_CA_PATH = os.path.join('proxy-certs', 'pywb-ca.pem')

    ALL_DIGITS = re.compile(r'^\d+$')

    def __init__(self, config_file='./config.yaml', custom_config=None):
        """
        :param str config_file: Path to the config file
        :param dict custom_config: Dictionary containing additional configuration information
        """
        self.handler = self.handle_request
        self.warcserver = WarcServer(config_file=config_file,
                                     custom_config=custom_config)

        config = self.warcserver.config

        self.debug = config.get('debug', False)

        self.warcserver_server = GeventServer(self.warcserver, port=0)

        self.proxy_prefix = None  # the URL prefix to be used for the collection with proxy mode (e.g. /coll/id_/)
        self.proxy_coll = None  # the name of the collection that has proxy mode enabled
        self.init_proxy(config)

        self.init_recorder(config.get('recorder'))

        self.init_autoindex(config.get('autoindex'))

        static_path = config.get('static_url_path', 'pywb/static/').replace('/', os.path.sep)
        self.static_handler = StaticHandler(static_path)

        self.cdx_api_endpoint = config.get('cdx_api_endpoint', '/cdx')

        self._init_routes()

        upstream_paths = self.get_upstream_paths(self.warcserver_server.port)

        framed_replay = config.get('framed_replay', True)
        self.rewriterapp = RewriterApp(framed_replay,
                                       config=config,
                                       paths=upstream_paths)

        self.templates_dir = config.get('templates_dir', 'templates')
        self.static_dir = config.get('static_dir', 'static')

        metadata_templ = os.path.join(self.warcserver.root_dir, '{coll}', 'metadata.yaml')
        self.metadata_cache = MetadataCache(metadata_templ)

    def _init_routes(self):
        """Initialize the routes and based on the configuration file makes available
        specific routes (proxy mode, record)"""
        self.url_map = Map()
        self.url_map.add(Rule('/static/_/<coll>/<path:filepath>', endpoint=self.serve_static))
        self.url_map.add(Rule('/static/<path:filepath>', endpoint=self.serve_static))
        self.url_map.add(Rule('/collinfo.json', endpoint=self.serve_listing))

        if self.is_valid_coll('$root'):
            coll_prefix = ''
        else:
            coll_prefix = '/<coll>'
            self.url_map.add(Rule('/', endpoint=self.serve_home))

        self.url_map.add(Rule(coll_prefix + self.cdx_api_endpoint, endpoint=self.serve_cdx))
        self.url_map.add(Rule(coll_prefix + '/', endpoint=self.serve_coll_page))
        self.url_map.add(Rule(coll_prefix + '/timemap/<timemap_output>/<path:url>', endpoint=self.serve_content))

        if self.recorder_path:
            self.url_map.add(Rule(coll_prefix + self.RECORD_ROUTE + '/<path:url>', endpoint=self.serve_record))

        if self.proxy_prefix is not None:
            # Add the proxy-fetch endpoint to enable PreservationWorker to make CORS fetches worry free in proxy mode
            self.url_map.add(Rule('/proxy-fetch/<path:url>', endpoint=self.proxy_fetch,
                                  methods=['GET', 'HEAD', 'OPTIONS']))
        self.url_map.add(Rule(coll_prefix + '/<path:url>', endpoint=self.serve_content))

    def get_upstream_paths(self, port):
        """Retrieve a dictionary containing the full URLs of the upstream apps

        :param int port: The port used by the replay and cdx servers
        :return: A dictionary containing the upstream paths (replay, cdx-server, record [if enabled])
        :rtype: dict[str, str]
        """
        base_paths = {
                'replay': self.REPLAY_API % port,
                'cdx-server': self.CDX_API % port,
               }

        if self.recorder_path:
            base_paths['record'] = self.recorder_path

        return base_paths

    def init_recorder(self, recorder_config):
        """Initialize the recording functionality of pywb. If recording_config is None this function is a no op"""
        if not recorder_config:
            self.recorder = None
            self.recorder_path = None
            return

        if isinstance(recorder_config, str):
            recorder_coll = recorder_config
            recorder_config = {}
        else:
            recorder_coll = recorder_config['souroe_coll']

        # TODO: support dedup
        dedup_index = None
        warc_writer = MultiFileWARCWriter(self.warcserver.archive_paths,
                                          max_size=int(recorder_config.get('rollover_size', 1000000000)),
                                          max_idle_secs=int(recorder_config.get('rollover_idle_secs', 600)),
                                          filename_template=recorder_config.get('filename_template'),
                                          dedup_index=dedup_index)

        self.recorder = RecorderApp(self.RECORD_SERVER % str(self.warcserver_server.port), warc_writer,
                                    accept_colls=recorder_config.get('souroe_filter'))


        recorder_server = GeventServer(self.recorder, port=0)

        self.recorder_path = self.RECORD_API % (recorder_server.port, recorder_coll)

    def init_autoindex(self, auto_interval):
        """Initialize and start the auto-indexing of the collections. If auto_interval is None this is a no op.

        :param str|int auto_interval: The auto-indexing interval from the configuration file or CLI argument
        """
        if not auto_interval:
            return

        from pywb.manager.autoindex import AutoIndexer

        colls_dir = self.warcserver.root_dir if self.warcserver.root_dir else None

        indexer = AutoIndexer(colls_dir=colls_dir, interval=int(auto_interval))

        if not os.path.isdir(indexer.root_path):
            msg = 'No managed directory "{0}" for auto-indexing'
            logging.error(msg.format(indexer.root_path))
            import sys
            sys.exit(2)

        msg = 'Auto-Indexing Enabled on "{0}", checking every {1} secs'
        logging.info(msg.format(indexer.root_path, auto_interval))
        indexer.start()

    def is_proxy_enabled(self, environ):
        return self.proxy_prefix is not None and 'wsgiprox.proxy_host' in environ

    def serve_home(self, environ):
        """Serves the home (/) view of pywb (not a collections)

        :param dict environ: The WSGI environment dictionary for the request
        :return: The WbResponse for serving the home (/) path
        :rtype: WbResponse
        """
        home_view = BaseInsertView(self.rewriterapp.jinja_env, 'index.html')
        fixed_routes = self.warcserver.list_fixed_routes()
        dynamic_routes = self.warcserver.list_dynamic_routes()

        routes = fixed_routes + dynamic_routes

        all_metadata = self.metadata_cache.get_all(dynamic_routes)

        content = home_view.render_to_string(environ,
                                             routes=routes,
                                             all_metadata=all_metadata)

        return WbResponse.text_response(content, content_type='text/html; charset="utf-8"')

    def serve_static(self, environ, coll='', filepath=''):
        """Serve a static file associated with a specific collection or one of pywb's own static assets

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The collection the static file is associated with
        :param str filepath: The file path (relative to the collection) for the static assest
        :return: The WbResponse for the static asset
        :rtype: WbResponse
        """
        proxy_enabled = self.is_proxy_enabled(environ)
        if proxy_enabled and environ.get('REQUEST_METHOD') == 'OPTIONS':
            return WbResponse.options_response(environ)
        if coll:
            path = os.path.join(self.warcserver.root_dir, coll, self.static_dir)
        else:
            path = self.static_dir

        environ['pywb.static_dir'] = path
        try:
            response = self.static_handler(environ, filepath)
            if proxy_enabled:
                response.add_access_control_headers(env=environ)
            return response
        except:
            self.raise_not_found(environ, 'Static File Not Found: {0}'.format(filepath))

    def get_metadata(self, coll):
        """Retrieve the metadata associated with a collection

        :param str coll: The name of the collection to receive metadata for
        :return: The collections metadata if it exists
        :rtype: dict
        """
        #if coll == self.all_coll:
        #    coll = '*'

        metadata = {'coll': coll,
                    'type': 'replay'}

        if coll in self.warcserver.list_fixed_routes():
            metadata.update(self.warcserver.get_coll_config(coll))
        else:
            metadata.update(self.metadata_cache.load(coll))

        return metadata

    def serve_coll_page(self, environ, coll='$root'):
        """Render and serve a collections search page (search.html).

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The name of the collection to serve the collections search page for
        :return: The WbResponse containing the collections search page
        :rtype: WbResponse
        """
        if not self.is_valid_coll(coll):
            self.raise_not_found(environ, 'No handler for "/{0}"'.format(coll))

        self.setup_paths(environ, coll)

        metadata = self.get_metadata(coll)

        view = BaseInsertView(self.rewriterapp.jinja_env, 'search.html')

        wb_prefix = environ.get('SCRIPT_NAME')
        if wb_prefix:
            wb_prefix += '/'

        content = view.render_to_string(environ,
                                        wb_prefix=wb_prefix,
                                        metadata=metadata,
                                        coll=coll)

        return WbResponse.text_response(content, content_type='text/html; charset="utf-8"')

    def serve_cdx(self, environ, coll='$root'):
        """Make the upstream CDX query for a collection and response with the results of the query

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The name of the collection this CDX query is for
        :return: The WbResponse containing the results of the CDX query
        :rtype: WbResponse
        """
        base_url = self.rewriterapp.paths['cdx-server']

        #if coll == self.all_coll:
        #    coll = '*'

        cdx_url = base_url.format(coll=coll)

        if environ.get('QUERY_STRING'):
            cdx_url += '&' if '?' in cdx_url else '?'
            cdx_url += environ.get('QUERY_STRING')

        try:
            res = requests.get(cdx_url, stream=True)

            content_type = res.headers.get('Content-Type')

            return WbResponse.bin_stream(StreamIter(res.raw),
                                         content_type=content_type)

        except Exception as e:
            return WbResponse.text_response('Error: ' + str(e), status='400 Bad Request')

    def serve_record(self, environ, coll='$root', url=''):
        """Serve a URL's content from a WARC/ARC record in replay mode or from the live web in
        live, proxy, and record mode.

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The name of the collection the record is to be served from
        :param str url: The URL for the corresponding record to be served if it exists
        :return: WbResponse containing the contents of the record/URL
        :rtype: WbResponse
        """
        if coll in self.warcserver.list_fixed_routes():
            return WbResponse.text_response('Error: Can Not Record Into Custom Collection "{0}"'.format(coll))

        return self.serve_content(environ, coll, url, record=True)

    def serve_content(self, environ, coll='$root', url='', timemap_output='', record=False):
        """Serve the contents of a URL/Record rewriting the contents of the response when applicable.

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The name of the collection the record is to be served from
        :param str url: The URL for the corresponding record to be served if it exists
        :param str timemap_output: The contents of the timemap included in the link header of the response
        :param bool record: Should the content being served by recorded (save to a warc). Only valid in record mode
        :return: WbResponse containing the contents of the record/URL
        :rtype: WbResponse
        """
        if not self.is_valid_coll(coll):
            self.raise_not_found(environ, 'No handler for "/{0}"'.format(coll))

        self.setup_paths(environ, coll, record)

        request_uri = environ.get('REQUEST_URI')
        script_name = environ.get('SCRIPT_NAME', '') + '/'
        if request_uri and request_uri.startswith(script_name):
            wb_url_str = request_uri[len(script_name):]

        else:
            wb_url_str = to_native_str(url)

            if environ.get('QUERY_STRING'):
                wb_url_str += '?' + environ.get('QUERY_STRING')

        metadata = self.get_metadata(coll)
        if record:
            metadata['type'] = 'record'

        if timemap_output:
            metadata['output'] = timemap_output
            # ensure that the timemap path information is not included
            wb_url_str = wb_url_str.replace('timemap/{0}/'.format(timemap_output), '')
        try:
            response = self.rewriterapp.render_content(wb_url_str, metadata, environ)
        except UpstreamException as ue:
            response = self.rewriterapp.handle_error(environ, ue)
            raise HTTPException(response=response)
        return response

    def setup_paths(self, environ, coll, record=False):
        """Populates the WSGI environment dictionary with the path information necessary to perform a response for
        content or record.

        :param dict environ: The WSGI environment dictionary for the request
        :param str coll: The name of the collection the record is to be served from
        :param bool record: Should the content being served by recorded (save to a warc). Only valid in record mode
        """
        if not coll or not self.warcserver.root_dir:
            return

        if coll != '$root':
            pop_path_info(environ)
            if record:
                pop_path_info(environ)

        paths = [self.warcserver.root_dir]

        if coll != '$root':
            paths.append(coll)

        paths.append(self.templates_dir)

        # jinja2 template paths always use '/' as separator
        environ['pywb.templates_dir'] = '/'.join(paths)

    def serve_listing(self, environ):
        """Serves the response for WARCServer fixed and dynamic listing (paths)

        :param dict environ: The WSGI environment dictionary for the request
        :return: WbResponse containing the frontend apps WARCServer URL paths
        :rtype: WbResponse
        """
        result = {'fixed': self.warcserver.list_fixed_routes(),
                  'dynamic': self.warcserver.list_dynamic_routes()
                 }

        return WbResponse.json_response(result)

    def is_valid_coll(self, coll):
        """Determines if the collection name for a request is valid (exists)

        :param str coll: The name of the collection to check
        :return: True if the collection is valid, false otherwise
        :rtype: bool
        """
        #if coll == self.all_coll:
        #    return True

        return (coll in self.warcserver.list_fixed_routes() or
                coll in self.warcserver.list_dynamic_routes())

    def raise_not_found(self, environ, msg):
        """Utility function for raising a werkzeug.exceptions.NotFound execption with the supplied WSGI environment
        and message.

        :param dict environ: The WSGI environment dictionary for the request
        :param str msg: The error message
        """
        raise NotFound(response=self.rewriterapp._error_response(environ, msg))

    def _check_refer_redirect(self, environ):
        """Returns a WbResponse for a HTTP 307 redirection if the HTTP referer header is the same as the HTTP host header

        :param dict environ: The WSGI environment dictionary for the request
        :return: WbResponse HTTP 307 redirection
        :rtype: WbResponse
        """
        referer = environ.get('HTTP_REFERER')
        if not referer:
            return

        host = environ.get('HTTP_HOST')
        if host not in referer:
            return

        inx = referer[1:].find('http')
        if not inx:
            inx = referer[1:].find('///')
            if inx > 0:
                inx + 1

        if inx < 0:
            return

        url = referer[inx + 1:]
        host = referer[:inx + 1]

        orig_url = environ['PATH_INFO']
        if environ.get('QUERY_STRING'):
            orig_url += '?' + environ['QUERY_STRING']

        full_url = host + urljoin(url, orig_url)
        return WbResponse.redir_response(full_url, '307 Redirect')

    def __call__(self, environ, start_response):
        return self.handler(environ, start_response)

    def handle_request(self, environ, start_response):
        """Retrieves the route handler and calls the handler returning its the response

        :param dict environ: The WSGI environment dictionary for the request
        :param start_response:
        :return: The WbResponse for the request
        :rtype: WbResponse
        """
        urls = self.url_map.bind_to_environ(environ)
        try:
            endpoint, args = urls.match()
            # store original script_name (original prefix) before modifications are made
            environ['pywb.app_prefix'] = environ.get('SCRIPT_NAME')

            response = endpoint(environ, **args)
            return response(environ, start_response)

        except HTTPException as e:
            redir = self._check_refer_redirect(environ)
            if redir:
                return redir(environ, start_response)

            return e(environ, start_response)

        except Exception as e:
            if self.debug:
                traceback.print_exc()

            response = self.rewriterapp._error_response(environ, 'Internal Error: ' + str(e), '500 Server Error')
            return response(environ, start_response)

    @classmethod
    def create_app(cls, port):
        """Create a new instance of FrontEndApp that listens on port with a hostname of 0.0.0.0

        :param int port: The port FrontEndApp is to listen on
        :return: A new instance of FrontEndApp wrapped in GeventServer
        :rtype: GeventServer
        """
        app = FrontEndApp()
        app_server = GeventServer(app, port=port, hostname='0.0.0.0')
        return app_server

    def init_proxy(self, config):
        """Initialize and start proxy mode. If proxy configuration entry is not contained in the config
        this is a no op. Causes handler to become an instance of WSGIProxMiddleware.

        :param dict config: The configuration object used to configure this instance of FrontEndApp
        """
        proxy_config = config.get('proxy')
        if not proxy_config:
            return

        if isinstance(proxy_config, str):
            proxy_coll = proxy_config
            proxy_config = {}
        else:
            proxy_coll = proxy_config['coll']

        if '/' in proxy_coll:
            raise Exception('Proxy collection can not contain "/"')

        proxy_config['ca_name'] = proxy_config.get('ca_name', self.PROXY_CA_NAME)
        proxy_config['ca_file_cache'] = proxy_config.get('ca_file_cache', self.PROXY_CA_PATH)

        if proxy_config.get('recording'):
            logging.info('Proxy recording into collection "{0}"'.format(proxy_coll))
            if proxy_coll in self.warcserver.list_fixed_routes():
                raise Exception('Can not record into fixed collection')

            proxy_coll += self.RECORD_ROUTE
            if not config.get('recorder'):
                config['recorder'] = 'live'

        else:
            logging.info('Proxy enabled for collection "{0}"'.format(proxy_coll))

        if proxy_config.get('enable_content_rewrite', True):
            self.proxy_prefix = '/{0}/bn_/'.format(proxy_coll)
        else:
            self.proxy_prefix = '/{0}/id_/'.format(proxy_coll)

        self.proxy_default_timestamp = proxy_config.get('default_timestamp')
        if self.proxy_default_timestamp:
            if not self.ALL_DIGITS.match(self.proxy_default_timestamp):
                try:
                    self.proxy_default_timestamp = iso_date_to_timestamp(self.proxy_default_timestamp)
                except:
                    raise Exception('Invalid Proxy Timestamp: Must Be All-Digit Timestamp or ISO Date Format')

        self.proxy_coll = proxy_coll

        self.handler = WSGIProxMiddleware(self.handle_request,
                                          self.proxy_route_request,
                                          proxy_host=proxy_config.get('host', 'pywb.proxy'),
                                          proxy_options=proxy_config)

    def proxy_route_request(self, url, environ):
        """ Return the full url that this proxy request will be routed to
        The 'environ' PATH_INFO and REQUEST_URI will be modified based on the returned url

        Default is to use the 'proxy_prefix' to point to the proxy collection
        """
        if self.proxy_default_timestamp:
            environ['pywb_proxy_default_timestamp'] = self.proxy_default_timestamp

        return self.proxy_prefix + url

    def proxy_fetch(self, env, url):
        """Proxy mode only endpoint that handles OPTIONS requests and COR fetches for Preservation Worker.

        Due to normal cross-origin browser restrictions in proxy mode, auto fetch worker cannot access the CSS rules
        of cross-origin style sheets and must re-fetch them in a manner that is CORS safe. This endpoint facilitates
        that by fetching the stylesheets for the auto fetch worker and then responds with its contents

        :param dict env: The WSGI environment dictionary
        :param str url:  The URL of the resource to be fetched
        :return: WbResponse that is either response to an Options request or the results of fetching url
        :rtype: WbResponse
        """
        if not self.is_proxy_enabled(env):
            # we are not in proxy mode so just respond with forbidden
            return WbResponse.text_response('proxy mode must be enabled to use this endpoint',
                                            status='403 Forbidden')

        if env.get('REQUEST_METHOD') == 'OPTIONS':
            return WbResponse.options_response(env)

        # ensure full URL
        request_url = env['REQUEST_URI']
        # replace with /id_ so we do not get rewritten
        url = request_url.replace('/proxy-fetch', '/id_')
        # update WSGI environment object
        env['REQUEST_URI'] = self.proxy_coll + url
        env['PATH_INFO'] = env['PATH_INFO'].replace('/proxy-fetch', self.proxy_coll + '/id_')
        # make request using normal serve_content
        response = self.serve_content(env, self.proxy_coll, url)
        # for WR
        if isinstance(response, WbResponse):
            response.add_access_control_headers(env=env)
        return response


# ============================================================================
class MetadataCache(object):
    """This class holds the collection medata template string and
    caches the metadata for a collection once it is rendered once.
    Cached metadata is updated if its corresponding file has been updated since last cache time (file mtime based)"""

    def __init__(self, template_str):
        """
        :param str template_str: The template string to be cached
        """
        self.template_str = template_str
        self.cache = {}

    def load(self, coll):
        """Load and receive the metadata associated with a collection.

        If the metadata for the collection is not cached yet its metadata file is read in and stored.
        If the cache has seen the collection before the mtime of the metadata file is checked and if it is more recent
        than the cached time, the cache is updated and returned otherwise the cached version is returned.

        :param str coll: Name of a collection
        :return: The cached metadata for a collection
        :rtype: dict
        """
        path = self.template_str.format(coll=coll)
        try:
            mtime = os.path.getmtime(path)
            obj = self.cache.get(path)
        except:
            return {}

        if not obj:
            return self.store_new(coll, path, mtime)

        cached_mtime, data = obj
        if mtime == cached_mtime == mtime:
            return obj

        return self.store_new(coll, path, mtime)

    def store_new(self, coll, path, mtime):
        """Load a collections metadata file and store it

        :param str coll: The name of the collection the metadata is for
        :param str path: The path to the collections metadata file
        :param float mtime: The current mtime of the collections metadata file
        :return: The collections metadata
        :rtype: dict
        """
        obj = load_yaml_config(path)
        self.cache[coll] = (mtime, obj)
        return obj

    def get_all(self, routes):
        """Load the metadata for all routes (collections) and populate the cache

        :param list[str] routes: List of collection names
        :return: A dictionary containing each collections metadata
        :rtype: dict
        """
        for route in routes:
            self.load(route)

        return {name: value[1] for name, value in iteritems(self.cache)}


# ============================================================================
if __name__ == "__main__":
    app_server = FrontEndApp.create_app(port=8080)
    app_server.join()


