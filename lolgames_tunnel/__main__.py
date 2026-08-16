import argparse
import asyncio

from .client import add_client_args, client


def main():
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest='cmd', required=True)
    add_client_args(subcommands.add_parser('client'))
    args = parser.parse_args()
    if args.cmd == 'client':
        asyncio.run(client(args))
    else:
        parser.error(f'unknown command: {args.cmd}')


if __name__ == '__main__':
    main()
