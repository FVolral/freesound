#
# Freesound is (c) MUSIC TECHNOLOGY GROUP, UNIVERSITAT POMPEU FABRA
#
# Freesound is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# Freesound is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#     See AUTHORS file.
#

import json
import logging

import gearman
import os
import csv
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from tickets import TICKET_STATUS_CLOSED
from tickets.models import Ticket
from sounds.models import BulkUploadProgress
from sounds.management.commands import csv_bulk_upload
from accounts.admin import FULL_DELETE_USER_ACTION_NAME, DELETE_USER_DELETE_SOUNDS_ACTION_NAME, \
    DELETE_USER_KEEP_SOUNDS_ACTION_NAME

logger = logging.getLogger("console")
logger_async_tasks = logging.getLogger('async_tasks')


class Command(BaseCommand):
    help = 'Run the async tasks worker'

    def write_stdout(self, msg):
        logger.info("[%d] %s" % (os.getpid(),msg))
        self.stdout.write(msg)
        self.stdout.flush()

    def handle(self, *args, **options):
        # N.B. don't take out the print statements as they're
        # very very very very very very very very very very
        # helpful in debugging supervisor+worker+gearman
        self.write_stdout('Initializing gm_worker\n')
        gm_worker = gearman.GearmanWorker(settings.GEARMAN_JOB_SERVERS)

        # Read all methods of the class and if it starts with 'task_' then
        # register as a task on gearman
        for task_name in dir(self):
            if task_name.startswith('task_'):
                task_name = str(task_name)
                t_name = task_name.replace('task_', '');
                self.write_stdout('Task: %s\n' % t_name)
                task_func = lambda i:(lambda x, y: getattr(Command, i)(self, x, y))
                gm_worker.register_task(t_name, task_func(task_name))

        self.write_stdout('Starting work\n')
        gm_worker.work()
        self.write_stdout('Ended work\n')

    def task_whitelist_user(self, gearman_worker, gearman_job):
        tickets = json.loads(gearman_job.data)
        self.write_stdout("Whitelisting users from tickets %i tickets)" % len(tickets))

        count_done = 0
        for ticket_id in tickets:
            ticket = Ticket.objects.get(id=ticket_id)
            whitelist_user = ticket.sender
            if not whitelist_user.profile.is_whitelisted:
                whitelist_user.profile.is_whitelisted = True
                whitelist_user.profile.save()
                pending_tickets = Ticket.objects.filter(sender=whitelist_user)\
                                                .exclude(status=TICKET_STATUS_CLOSED)
                # Set all sounds to OK and the tickets to closed
                for pending_ticket in pending_tickets:
                    if pending_ticket.sound:
                        pending_ticket.sound.change_moderation_state("OK")

                    # This could be done with a single update, but there's a chance
                    # we lose a sound that way (a newly created ticket who's sound
                    # is not set to OK, but the ticket is closed).
                    pending_ticket.status = TICKET_STATUS_CLOSED
                    pending_ticket.save()

                message = "Whitelisted user: %s" % whitelist_user.username
                self.write_stdout(message)
                logger_async_tasks.info(message)

            count_done = count_done + 1
            self.write_stdout("Finished processing one ticket, %d remaining" % (len(tickets)-count_done))
        return 'true' if len(tickets) == count_done else 'false'

    def task_delete_user(self, gearman_worker, gearman_job):
        self.write_stdout("Started delete_user task ")
        self.write_stdout("Data received: %s" % gearman_job.data)
        data = json.loads(gearman_job.data)
        user = User.objects.get(id=data['user_id'])

        try:
            if data['action'] == FULL_DELETE_USER_ACTION_NAME:
                # This will fully delete the user and the sounds from the database.
                # WARNING: Once the sounds are deleted NO DeletedSound object will
                # be created.
                user.delete()
                message = "Async delete user: %d (full delete)" % data['user_id']
                self.write_stdout(message)
                logger_async_tasks.info(message)
                return 'true'

            elif data['action'] == DELETE_USER_KEEP_SOUNDS_ACTION_NAME:
                # This will anonymize the user and will keep the sounds publicly
                # availabe
                user.profile.delete_user()
                message = "Async delete user: %d (sounds kept)" % data['user_id']
                self.write_stdout(message)
                logger_async_tasks.info(message)
                return 'true'

            elif data['action'] == DELETE_USER_DELETE_SOUNDS_ACTION_NAME:
                # This will anonymize the user and remove the sounds, a
                # DeletedSound object will be created for each sound but kill not
                # be publicly available
                user.profile.delete_user(True)
                message = "Async delete user: %d (including sounds)" % data['user_id']
                self.write_stdout(message)
                logger_async_tasks.info(message)
                return 'true'
        except Exception as e:
            # This exception is broad but we catch it so that we can log that an error happened.
            # TODO: catching more specific exceptions would be desirable
            message = "Error in async delete user: %d (%s)" % (data['user_id'], str(e))
            self.write_stdout(message)
            logger_async_tasks.info(message)

        return 'false'

    def task_bulk_describe_sound(self, gearman_worker, gearman_job):
        # Validate BulkUploadProgess obj with state N (initial state)
        self.write_stdout("Data received: %s" % gearman_job.data)
        try:
            bulk = BulkUploadProgress.objects.get(id=gearman_job.data)
            # Validate CSV file and update state
            reader = csv.reader(open(bulk.csv_path, 'rU'))
            reader.next() # Skip header
            lines = list(reader)

            cmd = csv_bulk_upload.Command()
            base_dir = os.path.join(settings.UPLOADS_PATH, str(bulk.user_id))
            valid_lines, errors = cmd.check_input_file(base_dir, lines, restrict_username=bulk.user.username)
            validation_errors = {}
            # validation_errors will contain the row data and the errors in the position 9
            for error in errors:
                line_number = error[0] - 2
                # if the line is not valid complete it with empty values
                while len(lines[line_number]) < 8:
                    lines[line_number].append("")
                validation_errors.setdefault(line_number, lines[line_number]+[[]])
                validation_errors[line_number][8].append(error[1])
            bulk.validation_errors = validation_errors.values()
            bulk.sounds_valid = len(valid_lines)
            bulk.progress_type = 'V'
            bulk.save()
            return 'true'
        except BulkUploadProgress.DoesNotExist as e:
            message = "Error in async validate Bulk describe: %s (%s)" % (data, str(e))
            self.write_stdout(message)
        return 'false'
