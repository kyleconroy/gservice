import io
import optparse
import os.path
import sys
import time
import types
import signal
import logging.config
import pwd

import setproctitle
import daemon
import daemon.daemon
import daemon.runner

from gservice import config

class RunnerStartException(Exception): pass

def main():
    """Entry point for serviced console script"""
    Runner().do_action()

def runner_options():
    parser = optparse.OptionParser()
    parser.add_option("-C", "--config", dest="config", metavar="<file>",
                    help="Path to Python script to configure and return service to run")
    parser.add_option("-X", "--extend", dest="extensions", metavar="<file/python>", action="append",
                    help="Python code or script path to extend over the config script", default = [])
    parser.add_option("-l", "--logfile", dest="logfile", metavar="<logfile>", default="serviced.log",
                    help="Log to a specified file, - for stdout (default: serviced.log)")
    parser.add_option("-p", "--pidfile", dest="pidfile", metavar="<pidfile>", default="serviced.pid",
                    help="Save pid in specified file (default: serviced.pid)")
    parser.add_option("-c", "--chroot", dest="chroot", metavar="<chroot>",
                    help="Chroot to a directory before running (default: don't chroot)")
    #parser.add_option("-d", "--rundir", dest="rundir", metavar="<directory>",
    #                help="Change to a directory before running, but after any chroot (default: .)")
    parser.add_option("-u", "--user", dest="user", metavar="<user>",
                    help="The user to run as. (default: don't change)")
    #parser.add_option("-g", "--group", dest="group", metavar="<group>",
    #                help="The group to run as. (default: don't change)")
    parser.add_option("-N", "--name", dest="name", metavar="<name>",
                    help="Name of the process using setprocname. (default: don't change)")
    #parser.add_option("-m", "--umask", dest="umask", metavar="<mask>",
    #                help="The (octal) file creation mask to apply. (default: 0077 if daemonized)")
    return parser

class Runner(daemon.runner.DaemonRunner):
    _args = sys.argv[1:]
    _opener = io.open
    
    logfile_path =      config.Option('logfile')
    pidfile_path =      config.Option('pidfile')
    proc_name =         config.Option('name')
    service_factory =   config.Option('service')
    chroot_path =       config.Option('chroot') 
    user =              config.Option('user')
    log_config =        config.Option('log_config')
    
    def __init__(self):
        self.action_funcs = {
            'start': '_start',
            'stop': '_stop',
            'restart': '_restart',
            'run': '_run',
            'reload': '_reload', }

        self.service = None
        self.app = self
        
        self.parse_args(self.load_config(runner_options()))
        # horrible hack, daemon tries to remove PID files owned by root
        # at exit by calling self.close().  We make that a noop so that
        # it won't raise a permission exception every time you call 'stop'
        daemon.DaemonContext.close = (lambda s: None)
        self.daemon_context = daemon.DaemonContext()
        self.daemon_context.stdin = self._open(self.stdin_path, 'r')
        self.daemon_context.stdout = self._open(
            self.stdout_path, 'a+', buffering=1)
        self.daemon_context.stderr = self._open(
            self.stderr_path, 'a+', buffering=1)

        self.pidfile = None
        if self.pidfile_abspath:
            pidfilepath = self.pidfile_abspath
            # workaround for bug in python-daemon that can not correctly
            # determine where the pid file is located when the chroot option
            # is specified
            if self.chroot_abspath:
                pidfilepath = os.path.join(self.chroot_abspath,
                                           self.pidfile_abspath[1:])
            self.pidfile = daemon.runner.make_pidlockfile(pidfilepath,
                self.pidfile_timeout)
        self.daemon_context.pidfile = self.pidfile
        self.daemon_context.chroot_directory = self.chroot_abspath

        # open the log files
        self._log_config()

        logger_files = set()
        # find all open filedescriptors opened for logging
        if self.log_config:
            for name, cfg in self.log_config.get("loggers").items():
                l = logging.getLogger(name)
                for h in l.handlers:
                    try:
                        logger_files.add(h.stream.fileno())
                    except AttributeError:
                        # handler doesn't have an open fd
                        pass

        # preserve open file descriptors used for logging
        self.daemon_context.files_preserve=list(logger_files)

    
    def load_config(self, parser):
        options, args = parser.parse_args(self._args)
        self.config_path = options.config
        
        def load_file(filename):
            f = self._open(filename, 'r')
            d = {'__file__': filename}
            exec f.read() in d,d
            return d

        if options.config:
            parser.set_defaults(**load_file(options.config))
        elif len(args) == 0 or args[0] in ['start', 'restart', 'run']:
            parser.error("a configuration file is required to start")

        for ex in options.extensions:
            try:
                parser.set_defaults(**load_file(ex))
            except IOError:
                # couldn't open the file try to interpret as python
                d = {}
                exec ex in d,d
                parser.set_defaults(**d)

        # Now we parse args again with the config file settings as defaults
        options, args = parser.parse_args(self._args)
        config.load(options.__dict__)
        return args
    
    def parse_args(self, args):
        try:
            self.action = args[0]
        except IndexError:
            self.action = 'run'
        
        self.stdin_path = '/dev/null'
        self.stdout_path = self.logfile_path
        self.stderr_path = self.logfile_path
        
        def abspath(f):
            return os.path.abspath(f) if f is not None else None

        self.pidfile_abspath = abspath(self.pidfile_path)
        self.pidfile_timeout = 3
        
        self.config_abspath = abspath(self.config_path)
        self.chroot_abspath = abspath(self.chroot_path)

        if self.action not in self.action_funcs:
            self._usage_exit(args)

        # convert user name into uid/gid pair
        self.uid = self.gid = None
        if self.user is not None:
            pw_record = pwd.getpwnam(self.user)
            self.uid = pw_record.pw_uid
            self.gid = pw_record.pw_gid
    
    def _log_config(self):
        if self.log_config:
            logging.config.dictConfig(self.log_config)
            
    def do_reload(self):
        self._log_config()
        self.service.reload()

    def _expand_service_generators(self, service_gen):

        children = []
        main_service = None

        if isinstance(service_gen, types.GeneratorType):
            helpful_exc_message = ("Invalid Generator.  Generators must yield a"
                                   " series of child dependencies as (name, "
                                   "service) pairs followed by a final yield "
                                   "containing only a service.")
            try:
                for cur in service_gen:
                    if isinstance(cur, tuple):
                        # be very explicit in checking tuples since we need to
                        # throw the exception *back* into the generator here, to
                        # make user debugging reasonable
                        if (len(cur) == 2 and
                            isinstance(cur[0], str) and
                            len(cur[0]) and
                            main_service is None):
                            children.append(cur)
                        else:
                            service_gen.throw(RunnerStartException, helpful_exc_message)
                    else:
                        main_service = cur
            except StopIteration, _:
                if main_service is None:
                    raise RunnerStartException(helpful_exc_message)
        else:
            main_service = service_gen

        return children, main_service

    def run(self):
        if ('gevent' in sys.modules and
           not config.Option('_allow_early_gevent_import_for_tests').value):
            sys.stderr.write("Fatal error: you cannot import gevent in your"
                             " configuration file.  Aborting.\n")
            raise SystemExit(1)
        
        # gevent complains if you import it before you daemonize
        import gevent
        gevent.signal(signal.SIGUSR1, self.do_reload)
        gevent.signal(signal.SIGTERM, self.terminate)

        if self._get_action_func() == '_run':
            # to make debugging easier, we're including the directory where
            # the configuration file lives as well as the current working
            # directory in the module search path
            sys.path.append(os.path.dirname(self.config_path))
            sys.path.append(os.getcwd())

        if self.proc_name:
            setproctitle.setproctitle(self.proc_name)

        service_gen = self.service_factory()

        children, main_service = self._expand_service_generators(service_gen)

        import gservice.rootservice
        self.service = gservice.rootservice.RootService(children,
            main_service)

        if hasattr(self.service, 'catch'):
            self.service.catch(SystemExit, lambda e,g: self.service.stop())

        def shed_privileges():
            if self.uid and self.gid:
                daemon.daemon.change_process_owner(self.uid, self.gid)
        self.service.serve_forever(ready_callback=shed_privileges)
    
    def terminate(self):
        # XXX: multiple SIGTERM signals should forcibly quit the process
        self.service.stop()

    def _reload(self):
        os.kill(int(self.pidfile.read_pid()), signal.SIGUSR1)

    def _start(self):
        # workaround for bug in python-daemon that can not correctly
        # determine where the pid file is located when the chroot option
        # is specified
        if self.pidfile_abspath:
            self.pidfile = daemon.runner.make_pidlockfile(
                self.pidfile_abspath, self.pidfile_timeout)
            self.daemon_context.pidfile = self.pidfile
        super(Runner, self)._start()

    def _run(self):
        print "Starting service..."
        self.run()
    
    def do_action(self, *args, **kwargs):
        func = self._get_action_func()
        getattr(self, func)(*args, **kwargs)

    def _open(self, *args, **kwargs):
        return self.__class__.__dict__['_opener'](*args, **kwargs)
