from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import VentureAnalysisSnapshot, VentureDocument, VentureOpportunity
from .services import import_informa_report, run_document_analysis


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
        self.assertContains(response, "Analisis de balances")
        self.assertContains(response, "Datos importados de Informa")

    def test_staff_user_can_upload_balance_pdf_for_analysis(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Ceramica Circular SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.CERAMICA,
            annual_revenue=Decimal("300000.00"),
            ebitda=Decimal("45000.00"),
            neos_fit_score=5,
            market_score=4,
            team_score=4,
            financial_score=4,
            risk_control_score=4,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(document, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=document.opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("84.00"),
                suggested_purchase_price=Decimal("180000.00"),
                summary="Encaja con Neos Ceramica.",
            )

        with patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "upload_balance",
                    "opportunity": str(opportunity.id),
                    "title": "Balance 2025",
                    "fiscal_year": "2025",
                    "file": SimpleUploadedFile("balance.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "use_ai": "on",
                },
            )

        self.assertRedirects(response, reverse("venture_studies:list"))
        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertEqual(document.title, "Balance 2025")
        self.assertEqual(document.fiscal_year, 2025)
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.recommendation, VentureAnalysisSnapshot.Recommendation.BUY)
        self.assertEqual(analysis.suggested_purchase_price, Decimal("180000.00"))

    def test_document_analysis_creates_snapshot_from_extracted_balance_text(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Aditivos Minerales SL",
            stage=VentureOpportunity.Stage.SCALEUP,
            status=VentureOpportunity.Status.DUE_DILIGENCE,
            strategic_fit=VentureOpportunity.StrategicFit.ADDITIVES,
            ticket_max=Decimal("50000.00"),
            neos_fit_score=5,
            market_score=5,
            team_score=4,
            financial_score=4,
            risk_control_score=4,
        )
        document = VentureDocument.objects.create(
            opportunity=opportunity,
            title="Balance 2025",
            file="venture_studies/test/balance.pdf",
            extracted_text=(
                "Importe neto de la cifra de negocios 420.000,00 "
                "EBITDA 80.000,00 Patrimonio neto 150.000,00 "
                "Deuda financiera 30.000,00 Tesoreria 10.000,00"
            ),
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )

        with patch("venture_studies.services.fetch_venture_web_context") as web_context:
            web_context.return_value = {
                "available": False,
                "note": "Sin contexto web en test.",
                "top_items": [],
                "website": {},
            }
            snapshot = run_document_analysis(document, use_ai=False)

        self.assertEqual(snapshot.opportunity, opportunity)
        self.assertEqual(snapshot.recommendation, VentureAnalysisSnapshot.Recommendation.BUY)
        self.assertGreater(snapshot.suggested_purchase_price, Decimal("0"))
        self.assertEqual(snapshot.annual_revenue, Decimal("420000.00"))
        self.assertEqual(snapshot.ebitda, Decimal("80000.00"))

    def test_informa_report_can_create_and_fill_company_fields(self):
        informa_text = """
        Denominacion Social
        ADITIVOS INFORMA SL
        CIF B12345678
        Domicilio social Calle Mayor 12
        Localidad Castellon
        Provincia Castellon
        CNAE 2399 Fabricacion de otros productos minerales
        Web www.aditivos-informa.example
        Telefono 964 123 456
        Email info@aditivos-informa.example
        Numero de empleados 18
        Importe neto de la cifra de negocios 420.000,00
        EBITDA 80.000,00
        """
        upload = SimpleUploadedFile("informa.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf")

        with patch("venture_studies.services.extract_pdf_text_from_file", return_value=informa_text):
            result = import_informa_report(upload, title="Informa Aditivos", overwrite_existing=True)

        opportunity = result["opportunity"]
        self.assertTrue(result["created"])
        self.assertEqual(opportunity.company_name, "ADITIVOS INFORMA SL")
        self.assertEqual(opportunity.tax_id, "B12345678")
        self.assertEqual(opportunity.website, "https://www.aditivos-informa.example")
        self.assertEqual(opportunity.cnae_code, "2399")
        self.assertEqual(opportunity.employees, 18)
        self.assertEqual(opportunity.annual_revenue, Decimal("420000.00"))
        self.assertEqual(opportunity.ebitda, Decimal("80000.00"))
        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertEqual(document.document_kind, VentureDocument.DocumentKind.INFORMA)

    def test_staff_user_can_upload_informa_report_from_page(self):
        informa_text = """
        Razon social
        CERAMICA INFORMA SL
        NIF B87654321
        Domicilio Calle Industria 4
        Provincia Castellon
        Actividad CNAE 2331 Fabricacion de azulejos
        """
        self.client.force_login(self.staff_user)

        with patch("venture_studies.services.extract_pdf_text_from_file", return_value=informa_text):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "upload_informa",
                    "title": "Informe Informa",
                    "file": SimpleUploadedFile("informa.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "overwrite_existing": "on",
                },
            )

        self.assertRedirects(response, reverse("venture_studies:list"))
        opportunity = VentureOpportunity.objects.get(company_name="CERAMICA INFORMA SL")
        self.assertEqual(opportunity.tax_id, "B87654321")
        self.assertEqual(opportunity.cnae_code, "2331")
