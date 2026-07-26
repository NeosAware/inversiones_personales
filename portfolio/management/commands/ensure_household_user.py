import os

from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update a local household user."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--password")
        parser.add_argument("--password-env")
        parser.add_argument("--email", default="")
        parser.add_argument("--staff", action="store_true")
        parser.add_argument("--superuser", action="store_true")

    def handle(self, *args, **options):
        password = options.get("password")
        password_env = options.get("password_env")
        if password_env:
            password = os.environ.get(password_env)
            if not password:
                raise CommandError(f"La variable de entorno {password_env} no esta definida o esta vacia.")
        if not password:
            raise CommandError("Usa --password o --password-env para indicar la contrasena del usuario.")

        User = get_user_model()
        user, created = User.objects.get_or_create(username=options["username"], defaults={"email": options["email"]})
        if options["email"]:
            user.email = options["email"]
        user.is_staff = options["staff"] or options["superuser"]
        user.is_superuser = options["superuser"]
        user.set_password(password)
        user.save()
        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"User {user.username} {action} successfully."))
