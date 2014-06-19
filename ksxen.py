import os
import sys
import stat
import SimpleHTTPServer
import SocketServer
import argparse
import contextlib
import tempfile
import subprocess
import pystache
import requests
import logging
import logging.handlers
import random
import StringIO
import shlex
import zc.thread
import logutils
import logutils.colorize
import time
import select
import crypt
import netifaces
import re


MAC_ADDR = ':'.join(['00', '16', '3E'] +
                    ['%02X' % n for n in random.sample(range(0x7f), 3)])


logutils.colorize.ColorizingStreamHandler.level_map = {
    logging.DEBUG: (None, 'cyan', False),
    logging.INFO: (None, 'green', False),
    logging.WARNING: (None, 'yellow', False),
    logging.ERROR: (None, 'magenta', False),
    logging.CRITICAL: (None, 'red', False)
    }

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
color_handler = logutils.colorize.ColorizingStreamHandler()
color_handler.setLevel(logging.DEBUG)
fmt = logging.Formatter('%(asctime)-15s %(message)s')
color_handler.setFormatter(fmt)
log.addHandler(color_handler)


def is_unix_domain_socket(filename):
    return stat.S_ISSOCK(os.stat(filename).st_mode)


def join_url_parts(*parts):
    return '/'.join(x.strip('/') for x in parts)


def run(command_line, timeout=sys.maxint, fp=None):
    argslist = shlex.split(command_line)
    rpipe, wpipe = os.pipe()
    start_time = time.time()

    ps = subprocess.Popen(
        argslist, stdout=wpipe, stderr=wpipe, shell=False
        )

    while timeout > time.time() - start_time:
        rfds, wfds, xfds = select.select([rpipe], [], [], 0.1)
        for fd in rfds:
            data = os.read(fd, 10000)
            if fp is not None:
                fp.write(data)
            else:
                sys.stdout.write(data)
                sys.stdout.flush()

        if ps.poll() is not None:
            os.close(rpipe)
            os.close(wpipe)
            break

    if ps.returncode != 0:
        raise RunException(
            'Run(cmd=%s, retcode=%s)' % (command_line, ps.returncode)
            )


class RunException(Exception):
    '''error occurred'''


@contextlib.contextmanager
def cd(todir):
    origdir = os.getcwd()
    os.chdir(todir)
    try:
        yield
    finally:
        os.chdir(origdir)


def return_to_origin(fn):
    def decorator(*xs, **kw):
        origdir = os.getcwd()
        try:
            return fn(*xs, **kw)
        finally:
            if os.getcwd() != origdir:
                os.chdir(origdir)

    return decorator


def parse_args(args):
    parser = argparse.ArgumentParser(description='build vm')
    parser.add_argument('-c', '--cfg-path', help='xen config file')
    parser.add_argument('-i', '--img-path', help='image path')
    parser.add_argument('-u', '--install-url', help='install url')
    parser.add_argument('-s', '--disk-size', type=int, help='size of disk')
    parser.add_argument('-p', '--root-passwd', help='password of root')
    parser.add_argument('-b', '--bridge-name', help='name of bridge')
    parser.add_argument('-k', '--ksurl', help='url of ks template')
    parser.add_argument('-n', '--name', help='name of vm')
    parser.add_argument('-r', '--ram', help='ram of vm')
    parser.add_argument('-x', '--extra', help='extra commands in %post')

    return parser.parse_args(args)


def make_sparse(filename, size=1):
    '''gets the path of file and the size in MB
    raise an exeception if file exists
    '''
    if os.path.exists(filename):
        raise IOError('cant make sparse file, one exists already')

    with open(filename, 'wb') as fp:
        fp.seek(size * 1024 * 1024 - 1)
        fp.write('\0')


@return_to_origin
def main(args=None):

    #import pdb; pdb.set_trace()

    options = parse_args(args or sys.argv[1:])

    if os.geteuid() != 0:
        log.error('fuck off, be root')
        return 1

    img_path = os.path.abspath(options.img_path)
    cfg_path = os.path.abspath(options.cfg_path)
    root_passwd = crypt.crypt(options.root_passwd.strip(), '$1$redhat$')
    disk_size = options.disk_size
    bridge_name = options.bridge_name

    tempdir = tempfile.mkdtemp()
    log.info('making temporal dir %s and moving into it' % tempdir)
    os.chdir(tempdir)

    if os.path.exists(img_path):
        log.info('backing up prev image %s' % img_path)
        os.rename(img_path, '%s.old' % img_path)

    make_sparse(img_path, disk_size)

    port, httpd = serve(os.getcwd())
    hostname = netifaces.ifaddresses(bridge_name)[2][0]['addr']
    log.info('serving on http://%s:%s' % (hostname, port))

    # download boot files
    # this needs to support fedora and centos6 (maybe not c5)
    for fname in 'vmlinuz', 'initrd.img', 'upgrade.img':
        try:
            download_file(join_url_parts(
                options.install_url, 'images/pxeboot', fname
                ))
        except:
            log.warn('could not dl %s' % fname)

    # use config template for ks
    with open('ks.cfg', 'w') as fp:
        log.info('writing kickstart to ks.cfg')
        fp.write(pystache.render(
            requests.get(options.ksurl).text,
            dict(root_passwd=root_passwd,
                 install_url=options.install_url,
                 extra=options.extra)
            ))

    if os.path.exists(cfg_path):
        log.error('xen configuration file %s\n exists' % cfg_path)
        return 1

    with open(cfg_path, 'w') as fp:
        log.info('xen configuration file does not exit')
        fp.write(pystache.render(
            temp_cfgdata_templ,
            dict(ksurl='http://%s:%s/ks.cfg' % (hostname, port),
                 name=options.name,
                 ram=options.ram,
                 mac_addr=MAC_ADDR,
                 img_path=img_path,
                 tempdir=tempdir,
                 bridge=options.bridge_name)
            ))

    log.info('creating vm')
    run('/usr/sbin/xl create %s' % cfg_path)

    log.info('attaching to console')
    run('/usr/sbin/xl console %s' % options.name)

    with open(cfg_path, 'w') as fp:
        log.info('replacing temporal real cfg file')
        fp.write(pystache.render(
            real_cfgdata_templ,
            dict(name=options.name,
                 ram=options.ram,
                 bridge=options.bridge_name,
                 img_path=img_path,
                 mac_addr=MAC_ADDR)
            ))

    time.sleep(5)

    if not is_running(options.name):
        run('/usr/sbin/xl create %s' % cfg_path)
    else:
        log.error('looks like %s is running' % options.name)


def is_running(name):
    fp = StringIO.StringIO()
    run('/usr/sbin/xl list', fp=fp)

    name_re = re.search('^%s\s' % name,
                        fp.getvalue(), flags=re.MULTILINE)

    return name_re is not None


def serve(webdir):
    '''simple httpd server that serves
    out of a directory'''

    with cd(webdir):
        httpd = SocketServer.TCPServer(
            ('0.0.0.0', 0),
            SimpleHTTPServer.SimpleHTTPRequestHandler
            )

        port = httpd.socket.getsockname()[1]
        zc.thread.Thread(httpd.serve_forever)

        return port, httpd


def download_file(uri, filename=None):
    """download a file
    """
    if filename is None:
        filename = uri.split('/')[-1]

    req = requests.get(uri)
    with open(filename, 'wb') as fp:
        for data in req.iter_content(chunk_size=1024):
            if data:
                fp.write(data)


temp_cfgdata_templ = '''\
kernel ="{{ tempdir }}/vmlinuz"
ramdisk = "{{ tempdir }}/initrd.img"
extra = "text ks={{ ksurl }}"
disk = ['tap:aio:{{ img_path }},xvda,w',]
name = "{{ name }}"
maxmem = {{ ram }}
memory = {{ ram }}
on_reboot = "destroy"
on_crash = "destroy"
vfb = []
vif = ["mac={{ mac_addr }},bridge={{ bridge }},script=vif-bridge",]
'''

real_cfgdata_templ = '''\
name = "{{ name }}"
maxmem = {{ ram }}
memory = {{ ram }}
bootloader = "pygrub"
on_poweroff = "destroy"
on_reboot = "restart"
on_crash = "restart"
disk = ['tap:aio:{{ img_path }},xvda,w',]
vfb = []
vif = ["mac={{ mac_addr }},bridge={{ bridge }},script=vif-bridge",]
'''

if __name__ == '__main__':
    sys.exit(main())
