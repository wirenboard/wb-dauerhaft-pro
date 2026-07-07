#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1].split("~")[0]


setup(
    name="wb-dauerhaft-pro",
    version=get_version(),
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="Wiren Board MQTT driver for Dauerhaft PRO RS-485 actuators",
    url="https://github.com/wirenboard/wb-dauerhaft-pro",
    license="MIT",
    packages=["wb_dauerhaft_pro"],
)
