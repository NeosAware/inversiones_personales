from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from portfolio.ownership import AssetOwnershipCategory

from .models import PropertyInvestment


class RealEstateViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="real-estate-user",
            password="StrongPass123!",
        )
        self.client.force_login(self.user)

    def test_can_create_property_with_owner_and_rent_from_page(self):
        response = self.client.post(
            reverse("real_estate:list"),
            {
                "ownership_category": AssetOwnershipCategory.XIMO,
                "property_name": "Monsenor Fernando Ferris",
                "city": "Castellon",
                "invested_equity": "85000,00",
                "market_value": "132000,00",
                "mortgage_balance": "12000,00",
                "annual_rent_income": "8400,00",
                "annual_expenses": "1400,00",
                "notes": "Piso alquilado",
            },
        )

        self.assertRedirects(response, reverse("real_estate:list"))
        property_item = PropertyInvestment.objects.get(property_name="Monsenor Fernando Ferris")
        self.assertEqual(property_item.ownership_category, AssetOwnershipCategory.XIMO)
        self.assertEqual(property_item.current_value, Decimal("120000.00"))
        self.assertEqual(property_item.annual_income, Decimal("7000.00"))

    def test_real_estate_page_shows_owner_breakdown(self):
        PropertyInvestment.objects.create(
            ownership_category=AssetOwnershipCategory.MONICA,
            property_name="Pintor Oliet 13 2o",
            city="Castellon",
            invested_equity=Decimal("80000.00"),
            market_value=Decimal("110000.00"),
            mortgage_balance=Decimal("10000.00"),
            annual_rent_income=Decimal("7200.00"),
            annual_expenses=Decimal("1200.00"),
        )

        response = self.client.get(reverse("real_estate:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Patrimonio inmobiliario por titular")
        self.assertContains(response, "Monica")
        self.assertContains(response, "Pintor Oliet 13 2o")
