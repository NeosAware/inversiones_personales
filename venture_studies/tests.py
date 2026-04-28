from decimal import Decimal
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import VentureAnalysisSnapshot, VentureDiscoveryCandidate, VentureDocument, VentureOpportunity
from .services import (
    discover_web_candidates,
    guess_company_name_from_upload,
    import_informa_report,
    parse_informa_company_fields,
    run_document_analysis,
    try_ai_venture_analysis,
)


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

        opportunity = VentureOpportunity.objects.get(company_name="Materiales Circulares SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
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
        self.assertContains(response, "Pestana de empresa")
        self.assertContains(response, "Subir informacion")
        self.assertContains(response, "Importar Informa y analizar")
        self.assertContains(response, "Analizar dossier con Claude")
        self.assertContains(response, "Analizar balance de esta empresa")
        self.assertContains(response, "Documentos de la empresa")
        self.assertContains(response, "Vigilancia web")

    def test_new_company_tab_shows_creation_form(self):
        VentureOpportunity.objects.create(company_name="Empresa Existente SL")
        self.client.force_login(self.staff_user)

        response = self.client.get(f"{reverse('venture_studies:list')}?new=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nueva pestana")
        self.assertContains(response, "PDF financiero/comercial para Claude")
        self.assertContains(response, "Subir PDF con informacion financiera y comercial")
        self.assertContains(response, "Si dejas Empresa vacio")
        self.assertContains(response, 'class="stack" novalidate')
        self.assertContains(response, "Guardar empresa")
        self.assertContains(response, 'href="?company=')

    def test_staff_user_can_create_company_with_initial_dossier_pdf(self):
        self.client.force_login(self.staff_user)

        def fake_analysis(document, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=document.opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("81.00"),
                suggested_purchase_price=Decimal("210000.00"),
                summary="Claude analiza el PDF inicial de alta.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis) as mocked_analysis:
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "save_opportunity",
                    "company_name": "Alta Con PDF SL",
                    "sector": "Materiales",
                    "geography": "Castellon",
                    "stage": VentureOpportunity.Stage.EARLY,
                    "status": VentureOpportunity.Status.SCREENING,
                    "strategic_fit": VentureOpportunity.StrategicFit.BOTH,
                    "identified_on": "2026-04-28",
                    "neos_fit_score": "4",
                    "market_score": "4",
                    "team_score": "3",
                    "financial_score": "3",
                    "risk_control_score": "3",
                    "title": "PDF financiero comercial",
                    "fiscal_year": "2026",
                    "use_ai": "on",
                    "file": SimpleUploadedFile("dossier.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                },
            )

        opportunity = VentureOpportunity.objects.get(company_name="Alta Con PDF SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertEqual(document.document_kind, VentureDocument.DocumentKind.DOSSIER)
        self.assertEqual(document.title, "PDF financiero comercial")
        mocked_analysis.assert_called_once_with(document, use_ai=True)

    def test_staff_user_can_create_company_from_initial_pdf_without_typing_company_name(self):
        self.client.force_login(self.staff_user)

        def fake_analysis(document, use_ai=True):
            opportunity = document.opportunity
            opportunity.tax_id = "B44556677"
            opportunity.sector = "Complejos ceramicos"
            opportunity.annual_revenue = Decimal("640000.00")
            opportunity.fit_summary = "Ficha completada desde el PDF inicial."
            opportunity.save()
            return VentureAnalysisSnapshot.objects.create(
                opportunity=opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("82.00"),
                suggested_purchase_price=Decimal("250000.00"),
                summary="Claude completa la ficha desde el PDF.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis) as mocked_analysis:
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "save_opportunity",
                    "stage": VentureOpportunity.Stage.EARLY,
                    "status": VentureOpportunity.Status.SCREENING,
                    "strategic_fit": VentureOpportunity.StrategicFit.BOTH,
                    "identified_on": "2026-04-28",
                    "neos_fit_score": "3",
                    "market_score": "3",
                    "team_score": "3",
                    "financial_score": "3",
                    "risk_control_score": "3",
                    "title": "Dossier inicial",
                    "use_ai": "on",
                    "file": SimpleUploadedFile(
                        "Informe financiero y comercial - COMPLEJOS CERAMICOS SL.pdf",
                        b"%PDF-1.4\n%%EOF",
                        content_type="application/pdf",
                    ),
                },
            )

        opportunity = VentureOpportunity.objects.get(company_name="COMPLEJOS CERAMICOS SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        self.assertEqual(opportunity.tax_id, "B44556677")
        self.assertEqual(opportunity.sector, "Complejos ceramicos")
        self.assertEqual(opportunity.annual_revenue, Decimal("640000.00"))
        self.assertEqual(opportunity.fit_summary, "Ficha completada desde el PDF inicial.")
        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertEqual(document.document_kind, VentureDocument.DocumentKind.DOSSIER)
        mocked_analysis.assert_called_once_with(document, use_ai=True)

    def test_initial_pdf_upload_does_not_500_when_analysis_fails(self):
        self.client.force_login(self.staff_user)

        with (
            patch("venture_studies.views.run_document_analysis", side_effect=RuntimeError("Claude timeout")),
            patch("venture_studies.views.logger.exception"),
        ):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "save_opportunity",
                    "stage": VentureOpportunity.Stage.EARLY,
                    "status": VentureOpportunity.Status.SCREENING,
                    "strategic_fit": VentureOpportunity.StrategicFit.BOTH,
                    "identified_on": "2026-04-28",
                    "neos_fit_score": "3",
                    "market_score": "3",
                    "team_score": "3",
                    "financial_score": "3",
                    "risk_control_score": "3",
                    "title": "Dossier con fallo",
                    "use_ai": "on",
                    "file": SimpleUploadedFile(
                        "Informe Financiero - FALLO CLAUDE SL.pdf",
                        b"%PDF-1.4\n%%EOF",
                        content_type="application/pdf",
                    ),
                },
            )

        opportunity = VentureOpportunity.objects.get(company_name="FALLO CLAUDE SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertIn("Claude timeout", document.extraction_error)

    def test_informa_parser_ignores_report_labels_and_chart_numbers(self):
        text = """
        DOMICILIO SOCIAL
        CARRETERA CASTELLON-TERUEL ,
        12110 L'ALCORA CASTELLON/CASTELLO
        TELEFONOS
        964362417
        EMAIL CORPORATIVO
        comcer@comcer.com
        PAGINA WEB
        www.comcer.com
        Empresa
        Sector
        COMPUESTOS CERAMICOS SL
        NIFB12462826 Numero D-U-N-S 862778222
        VENTAS BALANCE (2024)
        577.352 EUR
        RESULTADOS BALANCE (2024)
        -52.915 EUR
        ACTIVO TOTAL (2024)
        319.513 EUR
        ACTIVIDAD (CNAE 2009)
        4675
        Comercio al por mayor de productos quimicos
        EMPLEADOS
        4
        Bancos
        Entidad Sucursal Direccion Localidad Provincia
        BANCO BILBAO
        VIZCAYA
        EBITDA
        2020 2021 2022 2023 2024
        -500k
        0
        500k
        Highcharts.com
        """

        parsed = parse_informa_company_fields(text)

        self.assertEqual(parsed["company_name"], "COMPUESTOS CERAMICOS SL")
        self.assertEqual(parsed["legal_name"], "COMPUESTOS CERAMICOS SL")
        self.assertEqual(parsed["tax_id"], "B12462826")
        self.assertEqual(parsed["website"], "https://www.comcer.com")
        self.assertEqual(parsed["cnae_code"], "4675")
        self.assertEqual(parsed["cnae_label"], "Comercio al por mayor de productos quimicos")
        self.assertEqual(parsed["sector"], "Comercio al por mayor de productos quimicos")
        self.assertEqual(parsed["employees"], 4)
        self.assertEqual(parsed["annual_revenue"], Decimal("577352"))
        self.assertIsNone(parsed["ebitda"])
        self.assertNotIn("BANCO", parsed["geography"].upper())

    def test_company_name_guess_ignores_label_fallback_from_informa(self):
        uploaded_file = SimpleUploadedFile(
            "Informa Financiero - COMPUESTOS CERAMICOS SL.pdf",
            b"",
            content_type="application/pdf",
        )

        company_name = guess_company_name_from_upload(uploaded_file, fallback_name="Sector")

        self.assertEqual(company_name, "COMPUESTOS CERAMICOS SL")

    def test_staff_user_can_update_selected_company_from_partial_tab_form(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Empresa Parcial SL",
            sector="Ceramica",
            stage=VentureOpportunity.Stage.EARLY,
            status=VentureOpportunity.Status.SCREENING,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
            neos_fit_score=4,
            market_score=3,
            team_score=3,
            financial_score=2,
            risk_control_score=3,
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            f"{reverse('venture_studies:list')}?company={opportunity.id}",
            {
                "action": "save_opportunity",
                "opportunity_id": str(opportunity.id),
                "company_name": "Empresa Parcial Actualizada SL",
                "sector": "Aditivos ceramicos",
                "fit_summary": "Mejora el encaje con Neos Additives.",
            },
        )

        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.company_name, "Empresa Parcial Actualizada SL")
        self.assertEqual(opportunity.sector, "Aditivos ceramicos")
        self.assertEqual(opportunity.neos_fit_score, 4)
        self.assertEqual(opportunity.status, VentureOpportunity.Status.SCREENING)

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

        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        self.assertEqual(document.title, "Balance 2025")
        self.assertEqual(document.fiscal_year, 2025)
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.recommendation, VentureAnalysisSnapshot.Recommendation.BUY)
        self.assertEqual(analysis.suggested_purchase_price, Decimal("180000.00"))

    def test_staff_user_can_upload_financial_commercial_dossier_for_claude_analysis(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Dossier Comercial SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(document, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=document.opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.WATCH,
                confidence=VentureAnalysisSnapshot.Confidence.HIGH,
                score_pct=Decimal("68.00"),
                suggested_purchase_price=Decimal("95000.00"),
                summary="Claude revisa ventas, cartera comercial y tension de caja.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis) as mocked_analysis:
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "upload_dossier",
                    "opportunity": str(opportunity.id),
                    "title": "Dossier financiero y comercial",
                    "fiscal_year": "2026",
                    "file": SimpleUploadedFile("dossier.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "use_ai": "on",
                },
            )

        document = VentureDocument.objects.get(opportunity=opportunity)
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        self.assertEqual(document.document_kind, VentureDocument.DocumentKind.DOSSIER)
        self.assertEqual(document.title, "Dossier financiero y comercial")
        mocked_analysis.assert_called_once_with(document, use_ai=True)
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.agent_provider, "anthropic")
        self.assertEqual(analysis.agent_label, "Claude test")

    def test_staff_user_can_analyze_existing_document_from_document_table(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Documento Pendiente SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        document = VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.INFORMA,
            title="Informe ya cargado",
            file="venture_studies/test/informa.pdf",
            extracted_text="Ventas balance 500.000 EUR EBITDA 60.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(document, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=document.opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.WATCH,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("66.00"),
                suggested_purchase_price=Decimal("140000.00"),
                summary="Documento existente analizado.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis) as mocked_analysis:
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "analyze_document",
                    "document_id": str(document.id),
                },
            )

        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        mocked_analysis.assert_called_once_with(document, use_ai=True)
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.recommendation, VentureAnalysisSnapshot.Recommendation.WATCH)
        self.assertEqual(analysis.suggested_purchase_price, Decimal("140000.00"))

    def test_company_tab_prompts_analysis_when_documents_have_no_snapshot(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Pendiente Analisis SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance pendiente",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 500.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(f"{reverse('venture_studies:list')}?company={opportunity.id}")

        self.assertContains(response, "Hay PDFs cargados pendientes de valorar")
        self.assertContains(response, "Informacion clave")
        self.assertContains(response, "Analisis detallado de compra o participacion")
        self.assertContains(response, "Generar y descargar informe PDF")
        self.assertContains(response, "Descargar PDF")
        self.assertContains(response, "Pendiente Analisis SL")

    def test_company_tab_shows_investment_memo_after_analysis(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Memo Inversion SL",
            annual_revenue=Decimal("550000.00"),
            ebitda=Decimal("70000.00"),
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        VentureAnalysisSnapshot.objects.create(
            opportunity=opportunity,
            recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
            confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
            score_pct=Decimal("81.00"),
            valuation_low=Decimal("180000.00"),
            valuation_base=Decimal("240000.00"),
            valuation_high=Decimal("300000.00"),
            suggested_purchase_price=Decimal("190000.00"),
            suggested_ticket=Decimal("50000.00"),
            target_ownership_pct=Decimal("26.32"),
            summary="Interesa participar con control de riesgos.",
            drivers=["Encaje industrial"],
            risks=["Validar deuda"],
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(f"{reverse('venture_studies:list')}?company={opportunity.id}")

        self.assertContains(response, "Memo de inversion de Memo Inversion SL")
        self.assertContains(response, "Ticket participacion")
        self.assertContains(response, "Motivos para comprar o participar")
        self.assertContains(response, "Riesgos y condiciones")
        self.assertContains(response, "Descargar informe PDF")
        self.assertContains(response, "Interesa participar con control de riesgos.")

    def test_staff_user_can_download_analysis_pdf_report(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Descarga Informe SL",
            tax_id="B99887766",
            sector="Aditivos industriales",
            annual_revenue=Decimal("610000.00"),
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        document = VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance 2024",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 610.000 EUR EBITDA 90.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        analysis = VentureAnalysisSnapshot.objects.create(
            opportunity=opportunity,
            source_document=document,
            recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
            confidence=VentureAnalysisSnapshot.Confidence.HIGH,
            score_pct=Decimal("84.00"),
            valuation_low=Decimal("240000.00"),
            valuation_base=Decimal("300000.00"),
            valuation_high=Decimal("360000.00"),
            suggested_purchase_price=Decimal("230000.00"),
            suggested_ticket=Decimal("60000.00"),
            target_ownership_pct=Decimal("26.09"),
            summary="Interesa comprar con margen de seguridad.",
            drivers=["Ventas recurrentes"],
            risks=["Revisar deuda"],
            assumptions=["Multiplo prudente"],
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("venture_studies:analysis_pdf", args=[analysis.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("informe-inversion-descarga-informe-sl", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_staff_user_can_download_uploaded_document_pdf(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            opportunity = VentureOpportunity.objects.create(
                company_name="Descarga Documento SL",
                stage=VentureOpportunity.Stage.GROWTH_ISSUES,
                status=VentureOpportunity.Status.RESEARCH,
                strategic_fit=VentureOpportunity.StrategicFit.BOTH,
            )
            document = VentureDocument.objects.create(
                opportunity=opportunity,
                document_kind=VentureDocument.DocumentKind.BALANCE,
                title="Balance 2024",
                extracted_text="Ventas balance 610.000 EUR",
                extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
            )
            document.file.save("balance.pdf", ContentFile(b"%PDF-1.4\n%%EOF"), save=True)
            self.client.force_login(self.staff_user)

            response = self.client.get(reverse("venture_studies:document_pdf", args=[document.id]))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertIn("descarga-documento-sl-balance-2024.pdf", response["Content-Disposition"])
            self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4\n%%EOF")

    def test_staff_user_can_analyze_all_company_documents_together(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Analisis Combinado SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        balance = VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance 2024",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 500.000 EUR EBITDA 60.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.INFORMA,
            title="Informe Informa",
            file="venture_studies/test/informa.pdf",
            extracted_text="CNAE 4675 Comercio al por mayor de productos quimicos",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(opportunity, documents, use_ai=True):
            documents = list(documents)
            snapshot = VentureAnalysisSnapshot.objects.create(
                opportunity=opportunity,
                source_document=documents[0],
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("80.00"),
                suggested_purchase_price=Decimal("220000.00"),
                summary="Analisis combinado generado.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )
            self.assertEqual(documents[0], balance)
            self.assertEqual(len(documents), 2)
            return snapshot

        with patch("venture_studies.views.run_opportunity_documents_analysis", side_effect=fake_analysis) as mocked_analysis:
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "analyze_opportunity_documents",
                    "opportunity_id": str(opportunity.id),
                },
            )

        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        mocked_analysis.assert_called_once()
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.recommendation, VentureAnalysisSnapshot.Recommendation.BUY)
        self.assertEqual(analysis.suggested_purchase_price, Decimal("220000.00"))

    def test_generate_combined_analysis_can_redirect_to_pdf_download(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Analisis Descargable SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance 2024",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 500.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(opportunity, documents, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=opportunity,
                source_document=list(documents)[0],
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("80.00"),
                suggested_purchase_price=Decimal("220000.00"),
                summary="Analisis descargable generado.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_opportunity_documents_analysis", side_effect=fake_analysis):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "analyze_opportunity_documents",
                    "opportunity_id": str(opportunity.id),
                    "download_pdf": "1",
                },
            )

        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertRedirects(response, reverse("venture_studies:analysis_pdf", args=[analysis.id]), fetch_redirect_response=False)

    def test_pending_report_button_generates_and_downloads_pdf(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Informe Directo SL",
            annual_revenue=Decimal("500000.00"),
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance 2024",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 500.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        def fake_analysis(opportunity, documents, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=opportunity,
                source_document=list(documents)[0],
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("80.00"),
                suggested_purchase_price=Decimal("220000.00"),
                summary="Informe directo generado.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with patch("venture_studies.views.run_opportunity_documents_analysis", side_effect=fake_analysis):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "download_opportunity_report_pdf",
                    "opportunity_id": str(opportunity.id),
                    "generate_analysis": "1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertTrue(VentureAnalysisSnapshot.objects.filter(opportunity=opportunity).exists())

    def test_pending_report_downloads_pdf_even_when_analysis_generation_fails(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Informe Pendiente SL",
            annual_revenue=Decimal("500000.00"),
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.BALANCE,
            title="Balance 2024",
            file="venture_studies/test/balance.pdf",
            extracted_text="Ventas balance 500.000 EUR",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        self.client.force_login(self.staff_user)

        with (
            patch("venture_studies.views.run_opportunity_documents_analysis", side_effect=RuntimeError("Claude timeout")),
            patch("venture_studies.views.logger.exception"),
        ):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "download_opportunity_report_pdf",
                    "opportunity_id": str(opportunity.id),
                    "generate_analysis": "1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

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

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-test",
        CLAUDE_MAX_TOKENS=512,
    )
    def test_claude_venture_analysis_receives_dossier_context(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Contexto Comercial SL",
            stage=VentureOpportunity.Stage.GROWTH_ISSUES,
            status=VentureOpportunity.Status.RESEARCH,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
        )
        document = VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.DOSSIER,
            title="Dossier comercial",
            file="venture_studies/test/dossier.pdf",
            extracted_text="Ventas recurrentes 300.000, pipeline comercial 120.000 y dependencia de dos clientes.",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )
        core_payload = {
            "recommendation": VentureAnalysisSnapshot.Recommendation.WATCH,
            "confidence": VentureAnalysisSnapshot.Confidence.MEDIUM,
            "score_pct": Decimal("60.00"),
            "valuation_low": Decimal("75000.00"),
            "valuation_base": Decimal("100000.00"),
            "valuation_high": Decimal("125000.00"),
            "suggested_purchase_price": Decimal("85000.00"),
            "suggested_ticket": Decimal("25000.00"),
            "target_ownership_pct": Decimal("25.00"),
            "annual_revenue": Decimal("300000.00"),
            "ebitda": None,
            "net_equity": None,
            "net_debt": None,
            "cash_need": None,
            "summary": "Lectura interna.",
            "valuation_note": "Valoracion interna.",
            "web_summary": "",
            "drivers": ["Pipeline comercial"],
            "risks": ["Dependencia de clientes"],
            "assumptions": ["Sin EBITDA"],
            "agent_provider": "core",
            "agent_label": "Analisis interno",
            "analysis_payload": {},
        }

        def fake_claude(config, *, system_prompt, user_prompt):
            self.assertIn("dossier financiero/comercial", system_prompt)
            self.assertIn('"kind":"Dossier financiero/comercial"', user_prompt)
            self.assertIn("pipeline comercial", user_prompt)
            self.assertIn("opportunity_updates", user_prompt)
            self.assertIn('"quality_score":"0.50"', user_prompt)
            return (
                {
                    "recommendation": "watch",
                    "confidence": "high",
                    "score_pct": "72.00",
                    "annual_revenue": "300000.00",
                    "summary": "Claude cruza finanzas y senales comerciales.",
                    "drivers": ["Pipeline comercial validable"],
                    "risks": ["Dependencia de dos clientes"],
                    "assumptions": ["Cifras extraidas del dossier"],
                    "opportunity_updates": {
                        "sector": "Aditivos ceramicos",
                        "status": "En analisis",
                        "financial_score": 4,
                    },
                },
                {"input_tokens": 100, "output_tokens": 50, "estimated_cost_usd": Decimal("0.0010")},
            )

        with patch("venture_studies.services.call_anthropic_agent", side_effect=fake_claude):
            payload = try_ai_venture_analysis(
                opportunity,
                {"annual_revenue": Decimal("300000.00")},
                {
                    "note": "",
                    "top_items": [{"title": "Senal web", "source": "Test", "tone": "positivo"}],
                    "website": {"available": True, "quality_score": Decimal("0.50")},
                },
                core_payload,
                document.extracted_text,
                document=document,
                enabled=True,
            )

        self.assertEqual(payload["agent_provider"], "anthropic")
        self.assertEqual(payload["agent_label"], "Claude claude-test")
        self.assertEqual(payload["confidence"], VentureAnalysisSnapshot.Confidence.HIGH)
        self.assertEqual(payload["score_pct"], Decimal("72.00"))
        self.assertEqual(payload["annual_revenue"], Decimal("300000.00"))
        self.assertEqual(payload["opportunity_updates"]["sector"], "Aditivos ceramicos")
        self.assertEqual(payload["opportunity_updates"]["status"], VentureOpportunity.Status.RESEARCH)

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-test",
        CLAUDE_MAX_TOKENS=512,
    )
    def test_claude_analysis_fills_company_form_fields(self):
        opportunity = VentureOpportunity.objects.create(
            company_name="Ficha Claude SL",
            stage=VentureOpportunity.Stage.EARLY,
            status=VentureOpportunity.Status.SCREENING,
            strategic_fit=VentureOpportunity.StrategicFit.BOTH,
            notes="Nota manual que no debe cambiar.",
        )
        document = VentureDocument.objects.create(
            opportunity=opportunity,
            document_kind=VentureDocument.DocumentKind.DOSSIER,
            title="Dossier comercial",
            file="venture_studies/test/dossier.pdf",
            extracted_text="CIF B11223344. Ventas 520.000 EBITDA 95.000. Fabrica en Castellon y canal esmalteras.",
            extraction_status=VentureDocument.ExtractionStatus.EXTRACTED,
        )

        def fake_claude(config, *, system_prompt, user_prompt):
            return (
                {
                    "recommendation": "buy",
                    "confidence": "high",
                    "score_pct": "86.00",
                    "suggested_purchase_price": "310000.00",
                    "valuation_base": "380000.00",
                    "suggested_ticket": "90000.00",
                    "annual_revenue": "520000.00",
                    "ebitda": "95000.00",
                    "summary": "Claude detecta buen encaje industrial y comercial.",
                    "drivers": ["Canal esmalteras"],
                    "risks": ["Dependencia comercial"],
                    "assumptions": ["Datos del dossier"],
                    "opportunity_updates": {
                        "legal_name": "FICHA CLAUDE SOCIEDAD LIMITADA",
                        "tax_id": "b11223344",
                        "website": "www.fichaclaude.example",
                        "sector": "Aditivos para ceramica",
                        "geography": "Castellon",
                        "address": "Calle Industria 7",
                        "employees": "18",
                        "stage": "Problemas de crecimiento",
                        "status": "En analisis",
                        "strategic_fit": "Neos Additives",
                        "annual_revenue": "520000.00",
                        "ebitda": "95000.00",
                        "estimated_valuation": "380000.00",
                        "ticket_max": "90000.00",
                        "neos_fit_score": 5,
                        "market_score": 4,
                        "team_score": 4,
                        "financial_score": 4,
                        "risk_control_score": 3,
                        "fit_summary": "Complementa la cartera de aditivos.",
                        "growth_issue": "Tiene demanda pero necesita caja comercial.",
                        "synergy_notes": "Canal compartido con Neos Additives.",
                        "diligence_notes": "Validar concentracion de clientes.",
                        "red_flags": "Dependencia de dos cuentas principales.",
                        "next_steps": "Pedir contratos y detalle de margen.",
                    },
                },
                {"input_tokens": 100, "output_tokens": 50, "estimated_cost_usd": Decimal("0.0010")},
            )

        with (
            patch("venture_studies.services.fetch_venture_web_context") as web_context,
            patch("venture_studies.services.call_anthropic_agent", side_effect=fake_claude),
        ):
            web_context.return_value = {
                "available": False,
                "note": "Sin contexto web en test.",
                "top_items": [],
                "website": {},
            }
            snapshot = run_document_analysis(document, use_ai=True)

        opportunity.refresh_from_db()
        self.assertEqual(opportunity.legal_name, "FICHA CLAUDE SOCIEDAD LIMITADA")
        self.assertEqual(opportunity.tax_id, "B11223344")
        self.assertEqual(opportunity.website, "https://www.fichaclaude.example")
        self.assertEqual(opportunity.sector, "Aditivos para ceramica")
        self.assertEqual(opportunity.geography, "Castellon")
        self.assertEqual(opportunity.employees, 18)
        self.assertEqual(opportunity.stage, VentureOpportunity.Stage.GROWTH_ISSUES)
        self.assertEqual(opportunity.status, VentureOpportunity.Status.RESEARCH)
        self.assertEqual(opportunity.strategic_fit, VentureOpportunity.StrategicFit.ADDITIVES)
        self.assertEqual(opportunity.annual_revenue, Decimal("520000.00"))
        self.assertEqual(opportunity.ebitda, Decimal("95000.00"))
        self.assertEqual(opportunity.estimated_valuation, Decimal("380000.00"))
        self.assertEqual(opportunity.ticket_max, Decimal("90000.00"))
        self.assertEqual(opportunity.neos_fit_score, 5)
        self.assertEqual(opportunity.financial_score, 4)
        self.assertEqual(opportunity.fit_summary, "Complementa la cartera de aditivos.")
        self.assertEqual(opportunity.notes, "Nota manual que no debe cambiar.")
        self.assertIn("legal_name", snapshot.analysis_payload["applied_opportunity_updates"])
        self.assertIn("estimated_valuation", snapshot.analysis_payload["applied_opportunity_updates"])

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

        def fake_analysis(document, use_ai=True):
            return VentureAnalysisSnapshot.objects.create(
                opportunity=document.opportunity,
                source_document=document,
                recommendation=VentureAnalysisSnapshot.Recommendation.BUY,
                confidence=VentureAnalysisSnapshot.Confidence.MEDIUM,
                score_pct=Decimal("78.00"),
                suggested_purchase_price=Decimal("175000.00"),
                summary="Informe Informa analizado.",
                agent_provider="anthropic",
                agent_label="Claude test",
            )

        with (
            patch("venture_studies.services.extract_pdf_text_from_file", return_value=informa_text),
            patch("venture_studies.views.run_document_analysis", side_effect=fake_analysis) as mocked_analysis,
        ):
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "upload_informa",
                    "title": "Informe Informa",
                    "file": SimpleUploadedFile("informa.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "overwrite_existing": "on",
                },
            )

        opportunity = VentureOpportunity.objects.get(company_name="CERAMICA INFORMA SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        self.assertEqual(opportunity.tax_id, "B87654321")
        self.assertEqual(opportunity.cnae_code, "2331")
        mocked_analysis.assert_called_once()
        analysis = VentureAnalysisSnapshot.objects.get(opportunity=opportunity)
        self.assertEqual(analysis.recommendation, VentureAnalysisSnapshot.Recommendation.BUY)
        self.assertEqual(analysis.suggested_purchase_price, Decimal("175000.00"))

    def test_staff_user_can_delete_opportunity_from_radar(self):
        opportunity = VentureOpportunity.objects.create(company_name="Eliminar SL")
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("venture_studies:list"),
            {
                "action": "delete_opportunity",
                "opportunity_id": str(opportunity.id),
            },
        )

        self.assertRedirects(response, reverse("venture_studies:list"))
        self.assertFalse(VentureOpportunity.objects.filter(company_name="Eliminar SL").exists())

    def test_staff_user_can_promote_web_candidate(self):
        candidate = VentureDiscoveryCandidate.objects.create(
            company_name="Candidato Web SL",
            sector="ceramica",
            geography="Castellon",
            source_url="https://example.com/noticia",
            score_pct=Decimal("82.00"),
            rationale="Encaja con ceramica y crecimiento.",
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("venture_studies:list"),
            {
                "action": "promote_candidate",
                "candidate_id": str(candidate.id),
            },
        )

        opportunity = VentureOpportunity.objects.get(company_name="Candidato Web SL")
        self.assertRedirects(response, f"{reverse('venture_studies:list')}?company={opportunity.id}")
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, VentureDiscoveryCandidate.Status.PROMOTED)
        self.assertEqual(candidate.promoted_opportunity, opportunity)

    def test_web_discovery_stores_long_google_news_links(self):
        long_link = "https://news.google.com/rss/articles/" + ("abc" * 120)

        with patch("venture_studies.services.fetch_news_signal_for_query") as fetch_signal:
            fetch_signal.return_value = {
                "available": True,
                "items": [
                    {
                        "title": "Candidato Largo SL amplia su planta en Castellon",
                        "description": "Empresa de materiales ceramicos con inversion industrial.",
                        "link": long_link,
                        "source": "Prensa",
                    }
                ],
            }
            result = discover_web_candidates(
                geography="Castellon",
                sector_focus="ceramica materiales",
                max_candidates=3,
            )

        self.assertEqual(result["created_count"], 1)
        candidate = VentureDiscoveryCandidate.objects.get(company_name="Candidato Largo SL")
        self.assertEqual(candidate.source_url, long_link)
        self.assertLessEqual(len(candidate.source_url), 1000)

    def test_web_discovery_returns_empty_signal_when_news_fetch_fails(self):
        with patch("venture_studies.services.fetch_news_signal_for_query", side_effect=TimeoutError("timeout")):
            result = discover_web_candidates(
                geography="Castellon",
                sector_focus="ceramica materiales",
                max_candidates=3,
            )

        self.assertFalse(result["signal"]["available"])
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["updated_count"], 0)
        self.assertFalse(VentureDiscoveryCandidate.objects.exists())

    def test_staff_user_can_launch_web_discovery(self):
        candidate = VentureDiscoveryCandidate.objects.create(
            company_name="Descubierta SL",
            sector="materiales",
            geography="Castellon",
            source_url="https://example.com/descubierta",
            score_pct=Decimal("75.00"),
        )
        self.client.force_login(self.staff_user)

        with patch("venture_studies.views.discover_web_candidates") as discovery:
            discovery.return_value = {
                "created_count": 1,
                "updated_count": 0,
                "candidates": [candidate],
            }
            response = self.client.post(
                reverse("venture_studies:list"),
                {
                    "action": "discover_web",
                    "geography": "Castellon",
                    "sector_focus": "ceramica materiales",
                    "max_candidates": "8",
                },
            )

        self.assertRedirects(response, reverse("venture_studies:list"))
        discovery.assert_called_once()
