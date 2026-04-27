from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import VentureOpportunity


class VentureStudiesViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff_user = User.objects.create_user(
            username="venture-admin",
            password="StrongPass123!",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="venture-reader",
            password="StrongPass123!",
        )

    def test_staff_user_can_create_opportunity_from_page(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("venture_studies:list"),
            {
                "company_name": "Materiales Circulares SL",
                "website": "https://example.com",
                "sector": "Reciclado ceramico",
                "geography": "Castellon",
                "stage": VentureOpportunity.Stage.GROWTH_ISSUES,
                "status": VentureOpportunity.Status.RESEARCH,
                "strategic_fit": VentureOpportunity.StrategicFit.BOTH,
                "contact_name": "Fundador",
                "source": "Contacto sectorial",
                "identified_on": "2026-04-27",
                "next_review_on": "2026-05-15",
                "ticket_min": "25.000,00",
                "ticket_max": "75.000,00",
                "estimated_valuation": "450.000,00",
                "annual_revenue": "180.000,00",
                "ebitda": "-20.000,00",
                "cash_need": "90.000,00",
                "neos_fit_score": "5",
                "market_score": "4",
                "team_score": "4",
                "financial_score": "3",
                "risk_control_score": "3",
                "fit_summary": "Puede reforzar la propuesta circular del grupo.",
                "growth_issue": "Tiene pedidos, pero falta caja para industrializar.",
                "synergy_notes": "Materia prima y canal industrial compartido.",
                "diligence_notes": "Validar deuda y contratos.",
                "red_flags": "Margen todavia inestable.",
                "next_steps": "Pedir cuentas y cap table.",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("venture_studies:list"))
        opportunity = VentureOpportunity.objects.get(company_name="Materiales Circulares SL")
        self.assertEqual(opportunity.ticket_min, Decimal("25000.00"))
        self.assertEqual(opportunity.score_total, 19)

    def test_non_staff_user_cannot_create_opportunity(self):
        self.client.force_login(self.regular_user)

        response = self.client.post(
            reverse("venture_studies:list"),
            {
                "company_name": "Empresa privada",
                "stage": VentureOpportunity.Stage.EARLY,
                "status": VentureOpportunity.Status.SCREENING,
                "strategic_fit": VentureOpportunity.StrategicFit.ADDITIVES,
                "identified_on": "2026-04-27",
                "neos_fit_score": "3",
                "market_score": "3",
                "team_score": "3",
                "financial_score": "3",
                "risk_control_score": "3",
            },
        )

        self.assertRedirects(response, reverse("venture_studies:list"))
        self.assertFalse(VentureOpportunity.objects.exists())

    def test_page_shows_dashboard_sections(self):
        VentureOpportunity.objects.create(
            company_name="Aditivos Funcionales SL",
            stage=VentureOpportunity.Stage.EARLY,
            status=VentureOpportunity.Status.DUE_DILIGENCE,
            strategic_fit=VentureOpportunity.StrategicFit.ADDITIVES,
            neos_fit_score=5,
            market_score=4,
            team_score=4,
            financial_score=4,
            risk_control_score=4,
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("venture_studies:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Radar de empresas no cotizadas")
        self.assertContains(response, "Aditivos Funcionales SL")
        self.assertContains(response, "Neos Additives")
