"""Employee repository for database operations.

Provides data access layer for employee CRUD operations with
persona-based field filtering.
"""

from db.models import Employee
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class EmployeeNotFoundError(Exception):
    """Raised when an employee is not found."""

    pass


class EmployeeRepository:
    """Repository for employee database operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session

    async def get_by_id(self, employee_id: int) -> Employee:
        """Get employee by ID.

        Args:
            employee_id: The employee's database ID

        Returns:
            Employee model instance

        Raises:
            EmployeeNotFoundError: If employee doesn't exist
        """
        result = await self.session.execute(select(Employee).where(Employee.id == employee_id))
        employee = result.scalar_one_or_none()

        if employee is None:
            raise EmployeeNotFoundError(f"Employee with ID {employee_id} not found")

        return employee

    async def get_by_employee_number(self, employee_number: str) -> Employee:
        """Get employee by employee number.

        Args:
            employee_number: The employee's HR-assigned number

        Returns:
            Employee model instance

        Raises:
            EmployeeNotFoundError: If employee doesn't exist
        """
        result = await self.session.execute(
            select(Employee).where(Employee.employee_number == employee_number)
        )
        employee = result.scalar_one_or_none()

        if employee is None:
            raise EmployeeNotFoundError(f"Employee with number {employee_number} not found")

        return employee

    async def list_all(self, *, status: str | None = None) -> list[Employee]:
        """List all employees with optional status filter.

        Args:
            status: Optional status filter (Active, Inactive, Terminated)

        Returns:
            List of Employee model instances
        """
        query = select(Employee)

        if status:
            query = query.where(Employee.status == status)

        query = query.order_by(Employee.last_name, Employee.first_name)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_direct_reports(self, supervisor_id: int) -> list[Employee]:
        """Get employees who report to a supervisor.

        Args:
            supervisor_id: The supervisor's employee ID

        Returns:
            List of Employee model instances
        """
        result = await self.session.execute(
            select(Employee)
            .where(Employee.supervisor_id == supervisor_id)
            .order_by(Employee.last_name, Employee.first_name)
        )
        return list(result.scalars().all())
