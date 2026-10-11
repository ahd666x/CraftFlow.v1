from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

STAFF_GROUPS = ['1', '2', '3', 'انبار']


class Command(BaseCommand):
    help = 'Add users to representative group'

    def add_arguments(self, parser):
        parser.add_argument('--usernames', nargs='*', default=None,
                            help='Only add these users to the group')
        parser.add_argument('--apply', action='store_true',
                            help='Without this flag only list users')

    def handle(self, *args, **opts):
        group, _ = Group.objects.get_or_create(name='نماینده')
        qs = User.objects.filter(is_active=True, is_superuser=False) \
                         .exclude(groups__name__in=STAFF_GROUPS) \
                         .exclude(groups=group)
        if opts['usernames']:
            qs = qs.filter(username__in=opts['usernames'])

        users = list(qs)
        for u in users:
            full_name = u.get_full_name()
            # Use repr for safe output on Windows
            if full_name:
                self.stdout.write('{} ({})'.format(repr(u.username), repr(full_name)))
            else:
                self.stdout.write(repr(u.username))

        if opts['apply']:
            for u in users:
                u.groups.add(group)
            self.stdout.write(self.style.SUCCESS('{} users added to representative group.'.format(len(users))))
        else:
            self.stdout.write('Preview mode: use --apply to apply changes.')