import argparse
import asyncio
import sys

from .client import add_client_args, client
from .server import server


def main():
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest='cmd', required=True)
    subcommands.add_parser('server')
    add_client_args(subcommands.add_parser('client'))
    args = parser.parse_args()
    if args.cmd == 'server':
        asyncio.run(server())
    elif args.cmd == 'client':
        asyncio.run(client(args))
    else:
        parser.error(f'unknown command: {args.cmd}')


if __name__ == '__main__':
    main()

