# -*- encoding: utf-8 -*-
##############################################################################
#
#    Odoo, Open Source Management Solution
#    This module copyright (C) 2010 - 2014 Savoir-faire Linux
#    (<http://www.savoirfairelinux.com>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import os
import shutil
import re
import pwd
import time
import psutil
import signal
from datetime import datetime, timedelta

from contextlib import closing

import logging
_logger = logging.getLogger(__name__)
_logger.setLevel(logging.DEBUG)

from openerp import models, api, SUPERUSER_ID, tools, sql_db
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT as DATETIME_FMT


def exp_list_posix_user():
    """Rewrite/simplified version of openerp.service.exp_list()
    Lists all databases owned by the current posix user instead of the db_user
    from odoo config.
    The reason for this is because runbot creates databases with the posix
    user.
    :returns list of databases owned by the posix user
    """
    chosen_template = tools.config['db_template']
    templates_list = {'template0', 'template1', 'postgres', chosen_template}
    db = sql_db.db_connect('postgres')
    with closing(db.cursor()) as cr:
        db_user = pwd.getpwuid(os.getuid())[0]
        cr.execute("""
SELECT datname
FROM pg_database
WHERE datdba=(
    SELECT usesysid
    FROM pg_user
    WHERE usename=%s
) AND datname NOT IN %s order by datname""", (db_user, tuple(templates_list)))
        res = [tools.ustr(name) for (name,) in cr.fetchall()]
    res.sort()
    return res


class RunbotRepo(models.Model):
    """Aggressively clean filesystem databases and processes."""
    _inherit = "runbot.repo"

    def __init__(self, pool, cr):
        """On reinitialisation of db, mark all builds untouched since more than 1 day
        as done"""
        super(RunbotRepo, self).__init__(pool, cr)
        runbot_build = pool['runbot.build']
        yesterday = (datetime.now() - timedelta(1)).strftime(DATETIME_FMT)
        domain = [('state', '!=', 'done'),
                  ('write_date', '<', yesterday),
                  ]
        ids = pool['runbot.build'].search(cr, SUPERUSER_ID, domain)
        if ids:
            _logger.info('marking %d builds as done', len(ids))
        runbot_build.write(cr, SUPERUSER_ID, ids, {'state': 'done'})

    @api.model
    def cron(self):
        """Overcharge cron, add clean up subroutine before the general cron."""
        self.clean_up()
        return super(RunbotRepo, self).cron()

    def clean_up(self):
        """Examines the build directory, identify leftover builds then
        call the cleans: filesystem, database, process

        Leftover builds will have state done
        Skip if the build directory hasn't been created yet
        """
        self.clean_up_pids()
        build_root = os.path.join(self.root(), 'build')
        if not os.path.exists(build_root):
            return
        build_dirs = set(os.listdir(build_root))
        valid_builds = [b.dest for b in self.env['runbot.build'].search([
            ('dest', 'in', list(build_dirs)),
            ('state', '!=', 'done')
        ])]
        _logger.debug("build_dirs = %s", build_dirs)
        _logger.debug("valid_builds = %s", valid_builds)
        for pattern in build_dirs.difference(valid_builds):
            _logger.info("Runbot Janitor Cleaning up Residue: %s", pattern)
            try:
                self.clean_up_database(pattern)
            except OSError as e:
                _logger.error('Error in database cleanup: %s', e)
            try:
                self.clean_up_process(pattern)
            except OSError as e:
                _logger.error('Error in process cleanup: %s', e)
            try:
                self.clean_up_filesystem(pattern)
            except OSError as e:
                _logger.error('Error in file system cleanup: %s', e)

    def clean_up_pids(self):
        """Kill all done pids which are still running
        """
        for build in self.env['runbot.build'].search([
            ('pid', 'in', psutil.pids()),
            ('state', '=', 'done')
        ]):
            _logger.debug("Killing pid %s", build.pid)
            try:
                os.kill(build.pid, signal.SIGKILL)
            except OSError, exc:
                _logger.warning('Could not kill pid %d: %s', build.pid, exc)
                build.pid = False

    def clean_up_database(self, pattern):
        """Drop all databases whose names match the directory names matching
        the pattern.

        :param pattern:string
        """
        runbot_build = self.env['runbot.build']
        regex = re.compile(r'{}.*'.format(pattern))
        db_list = exp_list_posix_user()
        time.sleep(1)  # Give time for the cursor to close properly
        for db_name in filter(regex.match, db_list):
            _logger.debug("Dropping %s", db_name)
            runbot_build.pg_dropdb(dbname=db_name)

    def clean_up_process(self, pattern):
        """Kill processes which run executables in those directories or are
        connected to databases matching the directory names matching
        the pattern.

        :param pattern: string
        """
        regex = re.compile(r'.*-d {}.*'.format(pattern))
        for process in psutil.process_iter():
            if regex.match(" ".join(process.cmdline())):
                _logger.debug("Killing pid %s", process.pid)
                process.kill()

    def clean_up_filesystem(self, pattern):
        """Delete the directory and its contents matching the pattern.

        If there are logs, delete everything except those

        :param pattern: string
        """
        pattern_path = os.path.join(self.root(), 'build', pattern)
        log_dir = os.path.join(pattern_path, 'logs')
        if os.path.isdir(log_dir) and os.listdir(log_dir):
            for name in os.listdir(pattern_path):
                path = os.path.join(pattern_path, name)
                if name == 'logs':
                    continue
                if os.path.isdir(path):
                        shutil.rmtree(path)
                else:
                    os.unlink(path)
        else:
            shutil.rmtree(pattern_path)
