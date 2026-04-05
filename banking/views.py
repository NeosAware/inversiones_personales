import uuid
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from portfolio.ownership import AssetOwnershipCategory

from .forms import (
    BankBalanceForm,
    BankConnectionForm,
    BankExternalAccountForm,
    BankInstitutionSearchForm,
    StatementUploadForm,
)
from .models import BankBalance, BankConnection, BankExternalAccount, BankInvestmentPosition, BankStatementImport
from .services import (
    build_banking_dashboard,
    build_open_banking_dashboard,
    build_uploaded_file_checksum,
    create_open_banking_connection,
    import_statement,
    search_gocardless_institutions,
    sync_open_banking_connection,
)


class BankBalanceListView(LoginRequiredMixin, TemplateView):
    template_name = "banking/bankbalance_list.html"

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
        context.setdefault("institution_search_form", BankInstitutionSearchForm())
        context.setdefault("connection_form", BankConnectionForm())
        context.setdefault("external_account_form", BankExternalAccountForm())
        context["ownership_choices"] = AssetOwnershipCategory.choices
        context["statement_kind_choices"] = BankStatementImport.StatementKind.choices
        open_banking = build_open_banking_dashboard()
        context["open_banking_summary"] = open_banking["summary"]
        context["bank_connections"] = open_banking["connections"]
        context["external_bank_accounts"] = open_banking["external_accounts"]
        context.setdefault("institution_results", [])
        context.update(dashboard)
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
        if action == "search_institutions":
            return self._search_institutions(request)
        if action == "create_connection":
            return self._create_connection(request)
        if action == "run_connection_sync":
            return self._run_connection_sync(request)
        if action == "update_external_account":
            return self._update_external_account(request)

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
            checksum = build_uploaded_file_checksum(uploaded_file)
            if BankStatementImport.objects.filter(file_checksum=checksum).exists():
                messages.warning(request, f"{uploaded_file.name} ya estaba importado y se ha omitido.")
                continue

            statement = BankStatementImport.objects.create(
                source_file=uploaded_file,
                source_filename=uploaded_file.name,
                file_checksum=checksum,
                statement_kind=statement_kind,
            )
            try:
                import_statement(statement)
                imported_count += 1
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
        statements = list(BankStatementImport.objects.all())
        deleted_count = len(statements)
        for statement in statements:
            statement.delete()

        if deleted_count:
            messages.success(
                request,
                f"Se han eliminado {deleted_count} extracto(s). El resumen bancario ha quedado limpio para una nueva importacion.",
            )
        else:
            messages.info(request, "No habia extractos importados para eliminar.")

        return redirect("banking:list")

    def _search_institutions(self, request):
        form = BankInstitutionSearchForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(institution_search_form=form)
            return self.render_to_response(context, status=400)

        try:
            institution_results = search_gocardless_institutions(
                country_code=form.cleaned_data["country_code"],
                query=form.cleaned_data["query"],
            )
        except ValidationError as exc:
            messages.error(request, str(exc))
            institution_results = []

        connection_form = BankConnectionForm(
            initial={
                "country_code": form.cleaned_data["country_code"],
            }
        )
        context = self.get_context_data(
            institution_search_form=form,
            connection_form=connection_form,
            institution_results=institution_results,
        )
        return self.render_to_response(context)

    def _create_connection(self, request):
        form = BankConnectionForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(connection_form=form)
            return self.render_to_response(context, status=400)

        connection = BankConnection.objects.create(
            ownership_category=form.cleaned_data["ownership_category"],
            institution_name=form.cleaned_data["institution_name"],
            institution_id=form.cleaned_data["institution_id"],
            country_code=form.cleaned_data["country_code"],
            reference=f"bank-{uuid.uuid4().hex[:24]}",
        )
        callback_url = request.build_absolute_uri(
            reverse("banking:connection_callback") + f"?connection_id={connection.id}"
        )
        try:
            requisition_link = create_open_banking_connection(connection, callback_url)
        except ValidationError as exc:
            connection.last_error = str(exc)
            connection.save(update_fields=["last_error", "updated_at"])
            messages.error(request, str(exc))
            return redirect("banking:list")

        messages.success(
            request,
            f"Conexion preparada para {connection.institution_name}. Autoriza ahora el acceso del banco y volveras automaticamente.",
        )
        return redirect(requisition_link)

    def _run_connection_sync(self, request):
        connection = get_object_or_404(BankConnection, pk=request.POST.get("connection_id"))
        try:
            result = sync_open_banking_connection(connection)
        except ValidationError as exc:
            messages.error(request, str(exc))
            return redirect("banking:list")
        except Exception as exc:
            messages.error(request, f"No se ha podido sincronizar {connection.institution_name}: {exc}")
            return redirect("banking:list")

        messages.success(
            request,
            f"Sincronizacion completada para {connection.institution_name}: "
            f"{result['external_accounts']} cuenta(s)/tarjeta(s) revisadas y {result['imported_statements']} periodo(s) actualizados.",
        )
        return redirect("banking:list")

    def _update_external_account(self, request):
        external_account = get_object_or_404(BankExternalAccount, pk=request.POST.get("external_account_id"))
        form = BankExternalAccountForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(external_account_form=form)
            return self.render_to_response(context, status=400)

        external_account.ownership_category = form.cleaned_data["ownership_category"]
        external_account.statement_kind = form.cleaned_data["statement_kind"]
        external_account.is_active = form.cleaned_data["is_active"]
        external_account.save(update_fields=["ownership_category", "statement_kind", "is_active", "updated_at"])
        external_account.statement_imports.update(
            ownership_category=external_account.ownership_category,
            statement_kind=external_account.statement_kind,
        )
        if external_account.statement_kind == BankStatementImport.StatementKind.ACCOUNT:
            BankBalance.objects.filter(account_name__iexact=external_account.account_label).update(
                ownership_category=external_account.ownership_category
            )
        else:
            BankBalance.objects.filter(
                account_name__iexact=external_account.account_label,
                notes="Saldo sincronizado automaticamente por Open Banking.",
            ).delete()
        messages.success(request, f"Configuracion actualizada para {external_account.account_label}.")
        return redirect("banking:list")


class BankConnectionCallbackView(LoginRequiredMixin, TemplateView):
    template_name = "banking/connection_callback.html"

    def get(self, request, *args, **kwargs):
        connection = get_object_or_404(BankConnection, pk=request.GET.get("connection_id"))
        try:
            result = sync_open_banking_connection(connection)
        except Exception as exc:
            connection.last_error = str(exc)
            connection.save(update_fields=["last_error", "updated_at"])
            messages.error(request, f"La autorizacion ha vuelto, pero la primera sincronizacion ha fallado: {exc}")
            return redirect("banking:list")

        messages.success(
            request,
            f"{connection.institution_name} ya esta conectada: "
            f"{result['external_accounts']} cuenta(s)/tarjeta(s) detectadas.",
        )
        return redirect("banking:list")
