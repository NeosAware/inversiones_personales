from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from .forms import StatementUploadForm
from .models import BankBalance, BankInvestmentPosition, BankStatementImport
from .services import build_banking_dashboard, build_uploaded_file_checksum, import_statement


class BankBalanceListView(LoginRequiredMixin, TemplateView):
    template_name = "banking/bankbalance_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        accounts = list(BankBalance.objects.all())
        investment_positions = list(BankInvestmentPosition.objects.all())
        context["page_title"] = "Bank balances"
        context["accounts"] = accounts
        context["investment_positions"] = investment_positions
        context["summary"] = {
            "invested_amount": sum((account.deposited_amount for account in accounts), Decimal("0")),
            "current_value": sum((account.current_balance for account in accounts), Decimal("0")),
            "annual_income": sum((account.annual_interest_income for account in accounts), Decimal("0")),
        }
        context["investment_summary"] = {
            "positions_count": len(investment_positions),
            "invested_amount": sum((position.invested_amount for position in investment_positions), Decimal("0")),
            "current_value": sum((position.current_value for position in investment_positions), Decimal("0")),
            "annual_income": sum((position.annual_income for position in investment_positions), Decimal("0")),
        }
        context.setdefault("form", StatementUploadForm())
        context.update(build_banking_dashboard())
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "import")
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
        imported_count = 0

        for uploaded_file in uploaded_files:
            checksum = build_uploaded_file_checksum(uploaded_file)
            if BankStatementImport.objects.filter(file_checksum=checksum).exists():
                messages.warning(request, f"{uploaded_file.name} was already imported and has been skipped.")
                continue

            statement = BankStatementImport.objects.create(
                source_file=uploaded_file,
                source_filename=uploaded_file.name,
                file_checksum=checksum,
            )
            try:
                import_statement(statement)
                imported_count += 1
            except Exception as exc:
                messages.error(request, f"{uploaded_file.name} could not be imported: {exc}")

        if imported_count:
            messages.success(request, f"{imported_count} statement(s) imported successfully.")
        elif uploaded_files:
            messages.info(request, "No new statements were imported.")

        return redirect("banking:list")

    def _delete_statement(self, request):
        statement = get_object_or_404(BankStatementImport, pk=request.POST.get("statement_id"))
        source_name = statement.source_filename
        statement.delete()
        messages.success(request, f"{source_name} was deleted. You can import it again now.")
        return redirect("banking:list")

    def _delete_all_statements(self, request):
        statements = list(BankStatementImport.objects.all())
        deleted_count = len(statements)
        for statement in statements:
            statement.delete()

        if deleted_count:
            messages.success(
                request,
                f"{deleted_count} statement(s) deleted. The banking summary is now clean and ready for a fresh import.",
            )
        else:
            messages.info(request, "There were no imported statements to delete.")

        return redirect("banking:list")
