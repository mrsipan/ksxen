from setuptools import setup

extras_require = dict(test=['mock', 'zope.testing'])

entry_points='''
[console_scripts]
ksxen = ksxen:main
'''

setup(
    name='ksxen',
    version='0',
    author='Ben Sanchez',
    description='kickstart a xen vm',
    py_modules=['ksxen'],
    entry_points=entry_points,
    install_requires=['pystache', 'zc.thread', 'requests', 'logutils', 'netifaces'],
    extras_require=extras_require,
    tests_require=extras_require['test'],
    package_dir={'': '.'},
    zip_safe=False
)
