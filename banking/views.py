from decimal import Decimal
from pathlib import Path
import secrets

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt

from portfolio.ownership import AssetOwnershipCategory

from .forms import (
    BankBalanceForm,
    ROBOT_BANK_SUGGESTIONS,
    RobotSetupAssistantForm,
    StatementUploadForm,
)
from .models import BankBalance, BankInvestmentPosition, BankStatementImport
from .services import (
    build_banking_dashboard,
    build_robot_import_dashboard,
    build_smart_cockpit_extras,
    get_statement_import_feedback,
    import_uploaded_statement_file,
)


class BankBalanceListView(LoginRequiredMixin, TemplateView):
    template_name = "banking/bankbalance_list.html"

    def _default_robot_setup_initial(self):
        return {
            "bank_name": "Banco Sabadell",
            "ownership_category": AssetOwnershipCategory.JOINT,
            "statement_kind": BankStatementImport.StatementKind.ACCOUNT,
            "account_label": "",
            "login_url": "",
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        investment_positions = list(BankInvestmentPosition.objects.all())
        dashboard = build_banking_dashboard()
        context["page_title"] = "Banca"
        context["investment_positions"] = investment_positions
        context["summary"] = dashboard["accounts_summary"]
        context["investment_summary"] = {
            "positions_count": len(investment_positions),
            "invested_amount": sum((position.invested_amount for position in investment_positions), Decimal("0")),
            "current_value": sum((position.current_value for position in investment_positions), Decimal("0")),
            "annual_income": sum((position.annual_income for position in investment_positions), Decimal("0")),
        }
        context.setdefault("form", StatementUploadForm())
        context.setdefault("account_form", BankBalanceForm())
        context["ownership_choices"] = AssetOwnershipCategory.choices
        context["statement_kind_choices"] = BankStatementImport.StatementKind.choices
        robot_dashboard = build_robot_import_dashboard()
        context["robot_summary"] = robot_dashboard["summary"]
        context["recent_robot_imports"] = robot_dashboard["recent_imports"]
        context["robot_bridge_url"] = "http://127.0.0.1:8765"
        context["robot_installer_url"] = reverse("banking:robot_installer")
        context["robot_local_import_url"] = reverse("banking:robot_local_import")
        context.setdefault("robot_setup_form", RobotSetupAssistantForm(initial=self._default_robot_setup_initial()))
        context["robot_bank_suggestions"] = ROBOT_BANK_SUGGESTIONS
        context.update(dashboard)
        cockpit_extras = build_smart_cockpit_extras(
            annual_overview=dashboard["annual_overview"],
            reconciled_summary=dashboard["reconciled_summary"],
            reconciled_monthly_summaries=dashboard["reconciled_monthly_summaries"],
            continuity_summary=dashboard["continuity_summary"],
            accounts_summary=dashboard["accounts_summary"],
        )
        context["trends"] = cockpit_extras["trends"]
        context["smart_alerts"] = cockpit_extras["alerts"]
        context["daily_burn_rate"] = cockpit_extras["daily_burn_rate"]
        context["projected_monthly_expense"] = cockpit_extras["projected_monthly_expense"]
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "import")
        if action == "save_account":
            return self._save_account(request)
        if action == "update_account_ownership":
            return self._update_account_ownership(request)
        if action == "update_statement_ownership":
            return self._update_statement_ownership(request)
        if action == "delete_statement":
            return self._delete_statement(request)
        if action == "delete_all_statements":
            return self._delete_all_statements(request)

        return self._import_statements(request)

    def _import_statements(self, request):
        form = StatementUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(form=form)
            return self.render_to_response(context, status=400)

        uploaded_files = form.cleaned_data["files"]
        statement_kind = form.cleaned_data["statement_kind"]
        imported_count = 0

        for uploaded_file in uploaded_files:
            try:
                statement, created = import_uploaded_statement_file(
                    uploaded_file,
                    statement_kind=statement_kind,
                )
                if created:
                    imported_count += 1
                    continuity = get_statement_import_feedback(statement)
                    if continuity["has_issues"]:
                        messages.warning(
                            request,
                            f"{statement.account_name}: {continuity['note']}",
                        )
                else:
                    messages.warning(request, f"{uploaded_file.name} ya estaba importado y se ha omitido.")
            except Exception as exc:
                messages.error(request, f"No se ha podido importar {uploaded_file.name}: {exc}")

        if imported_count:
            document_label = "documento(s)" if statement_kind == BankStatementImport.StatementKind.CARD else "extracto(s)"
            messages.success(request, f"Se han importado correctamente {imported_count} {document_label}.")
        elif uploaded_files:
            messages.info(request, "No se ha importado ningun extracto nuevo.")

        return redirect("banking:list")

    def _save_account(self, request):
        form = BankBalanceForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(account_form=form)
            return self.render_to_response(context, status=400)

        account, created = BankBalance.objects.update_or_create(
            institution=form.cleaned_data["institution"],
            account_name=form.cleaned_data["account_name"],
            defaults={
                "ownership_category": form.cleaned_data["ownership_category"],
                "deposited_amount": form.cleaned_data["deposited_amount"],
                "current_balance": form.cleaned_data["current_balance"],
                "annual_interest_income": form.cleaned_data["annual_interest_income"],
                "notes": form.cleaned_data["notes"],
            },
        )

        if created:
            messages.success(request, f"La cuenta {account.account_name} se ha creado correctamente.")
        else:
            messages.success(request, f"La cuenta {account.account_name} se ha actualizado correctamente.")

        synced_statements = BankStatementImport.objects.filter(
            statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            account_label__iexact=account.account_name,
        ).update(
            ownership_category=account.ownership_category
        )
        if synced_statements:
            messages.info(request, f"Tambien se ha sincronizado el titular en {synced_statements} extracto(s) relacionados.")
        return redirect("banking:list")

    def _parse_ownership_category(self, request) -> str:
        ownership_category = request.POST.get("ownership_category", "").strip()
        valid_values = {choice[0] for choice in AssetOwnershipCategory.choices}
        if ownership_category not in valid_values:
            raise ValidationError("El titular seleccionado no es valido.")
        return ownership_category

    def _update_account_ownership(self, request):
        account = get_object_or_404(BankBalance, pk=request.POST.get("account_id"))
        try:
            ownership_category = self._parse_ownership_category(request)
        except ValidationError as exc:
            messages.error(request, str(exc))
            return redirect("banking:list")

        account.ownership_category = ownership_category
        account.save(update_fields=["ownership_category", "updated_at"])
        synced_statements = BankStatementImport.objects.filter(
            statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            account_label__iexact=account.account_name,
        ).update(
            ownership_category=ownership_category
        )
        messages.success(request, f"Titular actualizado para la cuenta {account.account_name}.")
        if synced_statements:
            messages.info(request, f"Tambien se ha actualizado el titular en {synced_statements} extracto(s) relacionados.")
        return redirect("banking:list")

    def _update_statement_ownership(self, request):
        statement = get_object_or_404(BankStatementImport, pk=request.POST.get("statement_id"))
        try:
            ownership_category = self._parse_ownership_category(request)
        except ValidationError as exc:
            messages.error(request, str(exc))
            return redirect("banking:list")

        if statement.iban:
            updated = BankStatementImport.objects.filter(iban=statement.iban).update(ownership_category=ownership_category)
        elif statement.account_label:
            updated = BankStatementImport.objects.filter(account_label=statement.account_label).update(
                ownership_category=ownership_category
            )
        else:
            statement.ownership_category = ownership_category
            statement.save(update_fields=["ownership_category"])
            updated = 1

        synced_accounts = 0
        if statement.statement_kind == BankStatementImport.StatementKind.ACCOUNT:
            synced_accounts = BankBalance.objects.filter(account_name__iexact=statement.account_name).update(
                ownership_category=ownership_category
            )
        document_label = "tarjeta(s)" if statement.statement_kind == BankStatementImport.StatementKind.CARD else "extracto(s)"
        messages.success(request, f"Titular actualizado para {updated} {document_label} de {statement.account_name}.")
        if synced_accounts:
            messages.info(request, f"Tambien se ha actualizado el titular en {synced_accounts} cuenta(s) manuales.")
        return redirect("banking:list")

    def _delete_statement(self, request):
        statement = get_object_or_404(BankStatementImport, pk=request.POST.get("statement_id"))
        source_name = statement.source_filename
        statement.delete()
        messages.success(request, f"{source_name} se ha eliminado. Ya puedes volver a importarlo.")
        return redirect("banking:list")

    def _delete_all_statements(self, request):
        messages.error(
            request,
            "El borrado masivo de importaciones se ha desactivado para evitar errores. Elimina los extractos uno a uno desde la tabla final.",
        )
        return redirect("banking:list")

def _extract_robot_token(request) -> str:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (
        request.headers.get("X-Bank-Robot-Token", "").strip()
        or request.POST.get("token", "").strip()
    )


@csrf_exempt
def robot_statement_import_view(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    expected_token = settings.BANK_ROBOT_IMPORT_TOKEN
    if not expected_token:
        return JsonResponse(
            {"ok": False, "error": "BANK_ROBOT_IMPORT_TOKEN no esta configurado en el servidor."},
            status=503,
        )

    provided_token = _extract_robot_token(request)
    if not provided_token or not secrets.compare_digest(provided_token, expected_token):
        return JsonResponse({"ok": False, "error": "Token de robot no valido."}, status=403)

    uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not uploaded_files:
        return JsonResponse({"ok": False, "error": "No se ha enviado ningun fichero."}, status=400)

    statement_kind = request.POST.get("statement_kind", BankStatementImport.StatementKind.ACCOUNT).strip()
    valid_statement_kinds = {choice[0] for choice in BankStatementImport.StatementKind.choices}
    if statement_kind not in valid_statement_kinds:
        return JsonResponse({"ok": False, "error": "statement_kind no valido."}, status=400)

    ownership_category = request.POST.get("ownership_category", AssetOwnershipCategory.JOINT).strip()
    valid_ownership_values = {choice[0] for choice in AssetOwnershipCategory.choices}
    if ownership_category not in valid_ownership_values:
        return JsonResponse({"ok": False, "error": "ownership_category no valido."}, status=400)

    institution = request.POST.get("institution", "").strip()
    account_label = request.POST.get("account_label", "").strip()

    results = []
    imported_count = 0
    skipped_count = 0
    for uploaded_file in uploaded_files:
        try:
            statement, created = import_uploaded_statement_file(
                uploaded_file,
                statement_kind=statement_kind,
                ownership_category=ownership_category,
                import_source=BankStatementImport.ImportSource.ROBOT,
                institution=institution,
                account_label=account_label,
            )
        except Exception as exc:
            results.append(
                {
                    "filename": uploaded_file.name,
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue

        if created:
            imported_count += 1
        else:
            skipped_count += 1
        results.append(
            {
                "filename": uploaded_file.name,
                "ok": True,
                "created": created,
                "statement_id": statement.id,
                "statement_kind": statement.statement_kind,
                "ownership_category": statement.ownership_category,
                "continuity": get_statement_import_feedback(statement),
            }
        )

    status = 200 if all(item["ok"] for item in results) else 207
    return JsonResponse(
        {
            "ok": all(item["ok"] for item in results),
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "results": results,
        },
        status=status,
    )


robot_statement_import_view.login_required = False


@login_required
def local_bridge_statement_import_view(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not uploaded_files:
        return JsonResponse({"ok": False, "error": "No se ha enviado ningun fichero."}, status=400)

    statement_kind = request.POST.get("statement_kind", BankStatementImport.StatementKind.ACCOUNT).strip()
    valid_statement_kinds = {choice[0] for choice in BankStatementImport.StatementKind.choices}
    if statement_kind not in valid_statement_kinds:
        return JsonResponse({"ok": False, "error": "statement_kind no valido."}, status=400)

    ownership_category = request.POST.get("ownership_category", AssetOwnershipCategory.JOINT).strip()
    valid_ownership_values = {choice[0] for choice in AssetOwnershipCategory.choices}
    if ownership_category not in valid_ownership_values:
        return JsonResponse({"ok": False, "error": "ownership_category no valido."}, status=400)

    institution = request.POST.get("institution", "").strip()
    account_label = request.POST.get("account_label", "").strip()

    results = []
    imported_count = 0
    skipped_count = 0
    for uploaded_file in uploaded_files:
        try:
            statement, created = import_uploaded_statement_file(
                uploaded_file,
                statement_kind=statement_kind,
                ownership_category=ownership_category,
                import_source=BankStatementImport.ImportSource.ROBOT,
                institution=institution,
                account_label=account_label,
            )
        except Exception as exc:
            results.append(
                {
                    "filename": uploaded_file.name,
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue

        if created:
            imported_count += 1
        else:
            skipped_count += 1
        results.append(
            {
                "filename": uploaded_file.name,
                "ok": True,
                "created": created,
                "statement_id": statement.id,
                "statement_kind": statement.statement_kind,
                "ownership_category": statement.ownership_category,
                "continuity": get_statement_import_feedback(statement),
            }
        )

    status = 200 if all(item["ok"] for item in results) else 207
    return JsonResponse(
        {
            "ok": all(item["ok"] for item in results),
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "results": results,
        },
        status=status,
    )


@login_required
def robot_assistant_installer_view(request):
    installer_path = Path(settings.BASE_DIR) / "scripts" / "install_bank_robot_assistant.ps1"
    if not installer_path.exists():
        return JsonResponse({"ok": False, "error": "No se ha encontrado el instalador local."}, status=404)
    return FileResponse(
        installer_path.open("rb"),
        as_attachment=True,
        filename="instalar_robot_bancario.ps1",
        content_type="text/plain; charset=utf-8",
    )
