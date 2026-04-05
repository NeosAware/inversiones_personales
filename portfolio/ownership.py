from django.db import models


class AssetOwnershipCategory(models.TextChoices):
    JOINT = "joint", "Conjunta"
    XIMO = "ximo", "Ximo"
    MONICA = "monica", "Monica"
