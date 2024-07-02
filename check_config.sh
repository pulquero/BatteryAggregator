#!/bin/bash

if [ ! -f "/usr/lib/python3.8/site-packages/jsonschema/__init__.py" ]; then
    echo "This script will install required packages to use check_config."
    echo
    echo "The following packages will be installed:"
    echo "  - python3-pip"
    echo "  - jsonschema"
    echo
    echo "Do you want to continue? [y/N]"
    read -r response
    if [ "$response" != "y" ]; then
        echo "Aborted."
        exit 1
    fi

    if ! opkg list-installed | grep -q "python3-pip"; then
        echo "Update packages..."
        opkg update
        echo
    fi

    if ! opkg list-installed | grep -q "python3-pip"; then
        echo "Install python3-pip..."
        opkg install python3-pip
        echo
    fi

    # fastest way to check if jsonschema is installed
    if [ ! -f "/usr/lib/python3.8/site-packages/jsonschema/__init__.py" ]; then
        echo "Install jsonschema..."
        pip3 install jsonschema==4.17.3
        echo
    fi
fi

python3 /data/BatteryAggregator/check_config.py
