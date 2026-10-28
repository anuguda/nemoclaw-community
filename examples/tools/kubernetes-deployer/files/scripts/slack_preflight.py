# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check Slack identity, channel membership and Socket Mode without posting.

Use environment variables or hidden terminal prompts. Outputs only IDs and
scope names; socket URLs and tokens are never printed or persisted.
"""
from __future__ import annotations
import argparse
import getpass
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.parse import urlencode


def validate_identity(auth, bot, workspace, app):
    team = auth.get('team_id', '')
    actual_app = bot.get('bot', {}).get('app_id', '')
    if not re.fullmatch(r'T[A-Z0-9]+', team) or (workspace and workspace != team):
        raise ValueError('workspace ID mismatch: use a T-prefixed team ID, not the channel ID')
    if not re.fullmatch(r'A[A-Z0-9]+', actual_app) or (app and app != actual_app):
        raise ValueError('bot/app token mismatch: both tokens must belong to the same Slack app')
    return {'workspaceId': team, 'appId': actual_app, 'botUserId': auth.get('user_id', '')}


def api(method, token, fields=None):
    request = Request('https://slack.com/api/' + method,
        data=urlencode(fields or {}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(1024 * 1024 + 1)
            scopes = response.headers.get('x-oauth-scopes', '')
        if len(body) > 1024 * 1024:
            raise ValueError('response too large')
        document = json.loads(body)
    except Exception:
        raise ValueError('Slack transport/response failure for ' + method) from None
    if not document.get('ok'):
        # Slack error identifiers are bounded; do not echo arbitrary responses.
        error = document.get('error', 'unknown_error')
        if not isinstance(error, str) or not re.fullmatch(r'[a-z_]{1,80}', error):
            error = 'unknown_error'
        raise ValueError(method + ': ' + error)
    return document, scopes


def check(bot_token, app_token, channel, workspace='', app=''):
    if not bot_token.startswith('xoxb-') or not re.fullmatch(r'xapp-1-A[A-Z0-9]+-.+', app_token):
        raise ValueError('expected a bot token and an app-level Socket Mode token')
    token_app = app_token.split('-')[2]
    if app and token_app != app:
        raise ValueError('configured app ID does not match app-level token')
    if not re.fullmatch(r'[CG][A-Z0-9]+', channel):
        raise ValueError('expected a channel ID')
    auth, scopes = api('auth.test', bot_token)
    bot, _ = api('bots.info', bot_token, {'bot': auth.get('bot_id', '')})
    result = validate_identity(auth, bot, workspace, token_app)
    info, _ = api('conversations.info', bot_token, {'channel': channel})
    if not info.get('channel', {}).get('is_member'):
        raise ValueError('invite the bot to the alert channel before enabling it')
    if info.get('channel', {}).get('is_archived'):
        raise ValueError('alert channel is archived')
    required = {'chat:write', 'app_mentions:read', 'im:history',
                'groups:history' if info['channel'].get('is_private') else 'channels:history'}
    missing = required - set(scopes.split(','))
    if missing:
        raise ValueError('missing bot OAuth scopes: ' + ', '.join(sorted(missing)))
    socket, _ = api('apps.connections.open', app_token)
    if not str(socket.get('url', '')).startswith('wss://'):
        raise ValueError('Slack did not return a Socket Mode connection')
    result.update(channelId=channel, channelMember=True, socketMode=True,
                  eventSubscriptions='verify in Slack app settings; not proven by this check')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--workspace', default='')
    parser.add_argument('--app', default='')
    args = parser.parse_args()
    bot = os.environ.get('SLACK_BOT_TOKEN') or getpass.getpass('Slack bot token: ')
    app = os.environ.get('SLACK_APP_TOKEN') or getpass.getpass('Slack app token: ')
    try:
        print(json.dumps(check(bot, app, args.channel, args.workspace, args.app), indent=2))
    except ValueError as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
