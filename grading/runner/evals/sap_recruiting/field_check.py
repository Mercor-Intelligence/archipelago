"""SAP Recruiting Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any SAP Recruiting field without writing SQL. The dropdown selection maps to
pre-defined SQL queries that have been verified against the actual SAP Recruiting schema.

The verifier can check entities created by:
- Job requisition tools (create, update, close job requisitions)
- Job application tools (create, update, move applications through stages)
- Job posting tools (post to internal/external boards)
- Offer letter tools (create, send, track offer letters)
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# All queries are schema-accurate for SAP Recruiting
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # USER CHECKS
    # =========================================================================
    "User Count": {
        "query": "SELECT COUNT(*) FROM users",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of users",
    },
    "Active User Count": {
        "query": "SELECT COUNT(*) FROM users WHERE disabled = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active (non-disabled) users",
    },
    "User Exists by Username": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(user_name) = LOWER(:user_name)",
        "required_params": ["user_name"],
        "result_type": "boolean",
        "description": "Verify user exists by username",
    },
    "User Exists by Email": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(email) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Verify user exists by email address",
    },
    "User First Name": {
        "query": "SELECT first_name FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's first name by ID",
    },
    "User Last Name": {
        "query": "SELECT last_name FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's last name by ID",
    },
    "User Email": {
        "query": "SELECT email FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's email by ID",
    },
    "User Disabled Status": {
        "query": "SELECT disabled FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "boolean",
        "description": "Check if user is disabled (1 = disabled, 0 = active)",
    },
    # =========================================================================
    # JOB REQUISITION CHECKS
    # =========================================================================
    "Job Requisition Count": {
        "query": "SELECT COUNT(*) FROM job_requisitions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of job requisitions",
    },
    "Job Requisition Count by Status": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE internal_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count job requisitions by internal status (0=Draft, 1=Open, 2=Closed)",
    },
    "Open Job Requisition Count": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE internal_status = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count open job requisitions",
    },
    "Closed Job Requisition Count": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE internal_status = 2",
        "required_params": [],
        "result_type": "integer",
        "description": "Count closed job requisitions",
    },
    "Draft Job Requisition Count": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE internal_status = 0 OR is_draft = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count draft job requisitions",
    },
    "Job Requisition Exists": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Verify job requisition exists by ID",
    },
    "Job Requisition Exists by GUID": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE job_req_guid = :job_req_guid",
        "required_params": ["job_req_guid"],
        "result_type": "boolean",
        "description": "Verify job requisition exists by GUID",
    },
    "Job Requisition Status": {
        "query": "SELECT internal_status FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get job requisition internal status (0=Draft, 1=Open, 2=Closed)",
    },
    "Job Requisition Custom Status": {
        "query": "SELECT custom_status FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job requisition custom status",
    },
    "Job Requisition Title": {
        "query": "SELECT job_title FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job requisition title",
    },
    "Job Requisition External Title": {
        "query": "SELECT external_title FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job requisition external title",
    },
    "Job Requisition Department": {
        "query": "SELECT department FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job requisition department",
    },
    "Job Requisition Location": {
        "query": "SELECT location FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job requisition location",
    },
    "Job Requisition Number of Openings": {
        "query": "SELECT number_openings FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get number of openings for a job requisition",
    },
    "Job Requisition Openings Filled": {
        "query": "SELECT openings_filled FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get number of openings filled for a job requisition",
    },
    "Job Requisition Salary Min": {
        "query": "SELECT salary_min FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "decimal",
        "description": "Get minimum salary for a job requisition",
    },
    "Job Requisition Salary Max": {
        "query": "SELECT salary_max FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "decimal",
        "description": "Get maximum salary for a job requisition",
    },
    "Job Requisition Salary Base": {
        "query": "SELECT salary_base FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "decimal",
        "description": "Get base salary for a job requisition",
    },
    "Job Requisition Currency": {
        "query": "SELECT currency FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get currency code for a job requisition",
    },
    "Job Requisition Recruiter ID": {
        "query": "SELECT recruiter_id FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get recruiter ID for a job requisition",
    },
    "Job Requisition Hiring Manager ID": {
        "query": "SELECT hiring_manager_id FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get hiring manager ID for a job requisition",
    },
    "Job Requisition Coordinator ID": {
        "query": "SELECT coordinator_id FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Get coordinator ID for a job requisition",
    },
    "Job Requisition Job Code": {
        "query": "SELECT job_code FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get job code for a job requisition",
    },
    "Job Requisition Template ID": {
        "query": "SELECT template_id FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get template ID for a job requisition",
    },
    "Job Requisition Is Draft": {
        "query": "SELECT is_draft FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job requisition is a draft",
    },
    "Job Requisition Is Deleted": {
        "query": "SELECT deleted FROM job_requisitions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job requisition is deleted",
    },
    "Job Requisition Count by Recruiter": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE recruiter_id = :recruiter_id",
        "required_params": ["recruiter_id"],
        "result_type": "integer",
        "description": "Count job requisitions assigned to a specific recruiter",
    },
    "Job Requisition Count by Hiring Manager": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE hiring_manager_id = :hiring_manager_id",
        "required_params": ["hiring_manager_id"],
        "result_type": "integer",
        "description": "Count job requisitions for a specific hiring manager",
    },
    "Job Requisition Count by Department": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE department = :department",
        "required_params": ["department"],
        "result_type": "integer",
        "description": "Count job requisitions in a specific department",
    },
    "Job Requisition Count by Location": {
        "query": "SELECT COUNT(*) FROM job_requisitions WHERE location = :location",
        "required_params": ["location"],
        "result_type": "integer",
        "description": "Count job requisitions at a specific location",
    },
    # =========================================================================
    # JOB REQUISITION POSTING CHECKS
    # =========================================================================
    "Job Posting Count": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of job postings",
    },
    "Job Posting Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count postings for a specific job requisition",
    },
    "Job Posting Exists": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_posting_id = :job_posting_id",
        "required_params": ["job_posting_id"],
        "result_type": "boolean",
        "description": "Verify job posting exists by ID",
    },
    "Job Posting Exists by Board": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_req_id = :job_req_id AND board_id = :board_id",
        "required_params": ["job_req_id", "board_id"],
        "result_type": "boolean",
        "description": "Verify job is posted to a specific board",
    },
    "Job Posting Status": {
        "query": "SELECT posting_status FROM job_requisition_postings WHERE job_posting_id = :job_posting_id",
        "required_params": ["job_posting_id"],
        "result_type": "integer",
        "description": "Get posting status (0-7)",
    },
    "Job Posting Board Name": {
        "query": "SELECT board_name FROM job_requisition_postings WHERE job_posting_id = :job_posting_id",
        "required_params": ["job_posting_id"],
        "result_type": "string",
        "description": "Get board name for a job posting",
    },
    "Internal Posting Exists": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_req_id = :job_req_id AND board_id = '_internal'",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has an internal posting",
    },
    "External Posting Exists": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_req_id = :job_req_id AND board_id = '_external'",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has an external posting",
    },
    "Agency Posting Exists": {
        "query": "SELECT COUNT(*) FROM job_requisition_postings WHERE job_req_id = :job_req_id AND board_id = '_agency'",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has an agency posting",
    },
    # =========================================================================
    # JOB REQUISITION OPERATOR CHECKS
    # =========================================================================
    "Job Operator Count": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of job operators",
    },
    "Job Operator Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count operators for a specific job requisition",
    },
    "Job Operator Count by Role": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators WHERE job_req_id = :job_req_id AND role = :role",
        "required_params": ["job_req_id", "role"],
        "result_type": "integer",
        "description": "Count operators with a specific role for a job requisition",
    },
    "Job Operator Exists": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators WHERE job_req_id = :job_req_id AND user_id = :user_id",
        "required_params": ["job_req_id", "user_id"],
        "result_type": "boolean",
        "description": "Check if a user is an operator for a job requisition",
    },
    "Job Operator Role": {
        "query": "SELECT role FROM job_requisition_operators WHERE job_req_id = :job_req_id AND user_id = :user_id",
        "required_params": ["job_req_id", "user_id"],
        "result_type": "string",
        "description": "Get the role of a user for a job requisition",
    },
    "Job Has Primary Recruiter": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators WHERE job_req_id = :job_req_id AND role = 'recruiter' AND is_primary = 1",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has a primary recruiter assigned",
    },
    "Job Has Primary Hiring Manager": {
        "query": "SELECT COUNT(*) FROM job_requisition_operators WHERE job_req_id = :job_req_id AND role = 'hiring_manager' AND is_primary = 1",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has a primary hiring manager assigned",
    },
    # =========================================================================
    # JOB REQUISITION SCREENING QUESTION CHECKS
    # =========================================================================
    "Screening Question Count": {
        "query": "SELECT COUNT(*) FROM job_req_screening_questions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of screening questions",
    },
    "Screening Question Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_req_screening_questions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count screening questions for a specific job requisition",
    },
    "Screening Question Exists": {
        "query": "SELECT COUNT(*) FROM job_req_screening_questions WHERE id = :question_id",
        "required_params": ["question_id"],
        "result_type": "boolean",
        "description": "Verify screening question exists by ID",
    },
    "Screening Question Name": {
        "query": "SELECT question_name FROM job_req_screening_questions WHERE id = :question_id",
        "required_params": ["question_id"],
        "result_type": "string",
        "description": "Get screening question name",
    },
    "Screening Question Type": {
        "query": "SELECT question_type FROM job_req_screening_questions WHERE id = :question_id",
        "required_params": ["question_id"],
        "result_type": "string",
        "description": "Get screening question type (QUESTION_TEXT, QUESTION_MULTI_CHOICE, QUESTION_RATING, QUESTION_NUMERIC)",
    },
    "Screening Question Is Required": {
        "query": "SELECT required FROM job_req_screening_questions WHERE id = :question_id",
        "required_params": ["question_id"],
        "result_type": "boolean",
        "description": "Check if screening question is required",
    },
    "Required Screening Question Count": {
        "query": "SELECT COUNT(*) FROM job_req_screening_questions WHERE job_req_id = :job_req_id AND required = 1",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count required screening questions for a job requisition",
    },
    # =========================================================================
    # JOB APPLICATION CHECKS
    # =========================================================================
    "Application Count": {
        "query": "SELECT COUNT(*) FROM job_applications",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of job applications",
    },
    "Application Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_applications WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count applications for a specific job requisition",
    },
    "Application Count by Status": {
        "query": """
            SELECT COUNT(*) FROM job_applications ja
            JOIN job_application_status jas ON ja.current_status_id = jas.id
            WHERE jas.status_name = :status
        """,
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count applications by status (open, disqualified, closed, withdraw, saved, non-applied, draft application)",
    },
    "Open Application Count": {
        "query": """
            SELECT COUNT(*) FROM job_applications ja
            JOIN job_application_status jas ON ja.current_status_id = jas.id
            WHERE jas.status_name = 'open'
        """,
        "required_params": [],
        "result_type": "integer",
        "description": "Count open applications",
    },
    "Disqualified Application Count": {
        "query": """
            SELECT COUNT(*) FROM job_applications ja
            JOIN job_application_status jas ON ja.current_status_id = jas.id
            WHERE jas.status_name = 'disqualified'
        """,
        "required_params": [],
        "result_type": "integer",
        "description": "Count disqualified applications",
    },
    "Application Exists": {
        "query": "SELECT COUNT(*) FROM job_applications WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "boolean",
        "description": "Verify application exists by ID",
    },
    "Application Exists by Candidate": {
        "query": "SELECT COUNT(*) FROM job_applications WHERE candidate_id = :candidate_id AND job_req_id = :job_req_id",
        "required_params": ["candidate_id", "job_req_id"],
        "result_type": "boolean",
        "description": "Check if candidate has applied to a specific job",
    },
    "Application Status": {
        "query": """
            SELECT jas.status_name FROM job_applications ja
            JOIN job_application_status jas ON ja.current_status_id = jas.id
            WHERE ja.application_id = :application_id
        """,
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application status name",
    },
    "Application Candidate First Name": {
        "query": "SELECT first_name FROM job_applications WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get applicant's first name",
    },
    "Application Candidate Last Name": {
        "query": "SELECT last_name FROM job_applications WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get applicant's last name",
    },
    "Application Candidate Email": {
        "query": "SELECT email FROM job_applications WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get applicant's email",
    },
    "Application Source": {
        "query": "SELECT source FROM job_applications WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application source (e.g., LinkedIn, Referral, Direct)",
    },
    "Application Count by Source": {
        "query": "SELECT COUNT(*) FROM job_applications WHERE source = :source",
        "required_params": ["source"],
        "result_type": "integer",
        "description": "Count applications by source",
    },
    "Application Count by Candidate": {
        "query": "SELECT COUNT(*) FROM job_applications WHERE candidate_id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "integer",
        "description": "Count applications for a specific candidate",
    },
    # =========================================================================
    # APPLICATION STATUS AUDIT CHECKS
    # =========================================================================
    "Status Audit Count": {
        "query": "SELECT COUNT(*) FROM job_application_status_audit",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total status audit entries",
    },
    "Status Audit Count by Application": {
        "query": "SELECT COUNT(*) FROM job_application_status_audit WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count status changes for a specific application",
    },
    "Application Latest Status Change": {
        "query": """
            SELECT new_status_name FROM job_application_status_audit
            WHERE application_id = :application_id
            ORDER BY changed_at DESC LIMIT 1
        """,
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get the most recent status for an application",
    },
    # =========================================================================
    # APPLICATION QUESTION RESPONSE CHECKS
    # =========================================================================
    "Question Response Count": {
        "query": "SELECT COUNT(*) FROM job_application_question_responses",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total question responses",
    },
    "Question Response Count by Application": {
        "query": "SELECT COUNT(*) FROM job_application_question_responses WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count question responses for a specific application",
    },
    "Question Response Answer": {
        "query": """
            SELECT answer FROM job_application_question_responses
            WHERE application_id = :application_id AND "order" = :question_order
        """,
        "required_params": ["application_id", "question_order"],
        "result_type": "string",
        "description": "Get answer to a specific question for an application",
    },
    # =========================================================================
    # OFFER LETTER CHECKS
    # =========================================================================
    "Offer Letter Count": {
        "query": "SELECT COUNT(*) FROM offer_letters",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of offer letters",
    },
    "Offer Letter Count by Status": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count offer letters by status (draft, pending_approval, approved, rejected, sent, accepted, declined)",
    },
    "Offer Letter Exists": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "boolean",
        "description": "Verify offer letter exists by ID",
    },
    "Offer Letter Exists by Application": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "boolean",
        "description": "Check if an offer letter exists for an application",
    },
    "Offer Letter Status": {
        "query": "SELECT status FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "string",
        "description": "Get offer letter status (draft, pending_approval, approved, rejected, sent, accepted, declined)",
    },
    "Offer Letter Job Title": {
        "query": "SELECT job_title FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "string",
        "description": "Get job title in offer letter",
    },
    "Offer Letter Salary Rate": {
        "query": "SELECT salary_rate FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "decimal",
        "description": "Get salary rate in offer letter",
    },
    "Offer Letter Currency": {
        "query": "SELECT currency_code FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "string",
        "description": "Get currency code in offer letter",
    },
    "Offer Letter Target Bonus Percent": {
        "query": "SELECT target_bonus_percent FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "integer",
        "description": "Get target bonus percentage in offer letter",
    },
    "Offer Letter Target Bonus Amount": {
        "query": "SELECT target_bonus_amount FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "decimal",
        "description": "Get target bonus amount in offer letter",
    },
    "Offer Letter Stock Option": {
        "query": "SELECT stock_option FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "integer",
        "description": "Get stock option count in offer letter",
    },
    "Offer Letter Send Mode": {
        "query": "SELECT send_mode FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "string",
        "description": "Get send mode (email, emailaspdf, print, verbal, pending)",
    },
    "Offer Letter Start Date": {
        "query": "SELECT job_start_date FROM offer_letters WHERE offer_letter_id = :offer_letter_id",
        "required_params": ["offer_letter_id"],
        "result_type": "string",
        "description": "Get job start date in offer letter",
    },
    "Accepted Offer Letter Count": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE status = 'accepted'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count accepted offer letters",
    },
    "Declined Offer Letter Count": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE status = 'declined'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count declined offer letters",
    },
    "Pending Approval Offer Letter Count": {
        "query": "SELECT COUNT(*) FROM offer_letters WHERE status = 'pending_approval'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count offer letters pending approval",
    },
    # =========================================================================
    # JOB REQUISITION LOCALE CHECKS
    # =========================================================================
    "Job Locale Count": {
        "query": "SELECT COUNT(*) FROM job_requisition_locales",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total job locale entries",
    },
    "Job Locale Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_requisition_locales WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count locale entries for a specific job requisition",
    },
    "Job Locale Title": {
        "query": "SELECT job_title FROM job_requisition_locales WHERE job_req_id = :job_req_id AND locale = :locale",
        "required_params": ["job_req_id", "locale"],
        "result_type": "string",
        "description": "Get job title for a specific locale",
    },
    "Job Locale Description": {
        "query": "SELECT job_description FROM job_requisition_locales WHERE job_req_id = :job_req_id AND locale = :locale",
        "required_params": ["job_req_id", "locale"],
        "result_type": "string",
        "description": "Get job description for a specific locale",
    },
    "Job Has Locale": {
        "query": "SELECT COUNT(*) FROM job_requisition_locales WHERE job_req_id = :job_req_id AND locale = :locale",
        "required_params": ["job_req_id", "locale"],
        "result_type": "boolean",
        "description": "Check if job has a specific locale",
    },
    # =========================================================================
    # JOB REQUISITION POSITION CHECKS
    # =========================================================================
    "Job Position Count": {
        "query": "SELECT COUNT(*) FROM job_req_positions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total job position entries",
    },
    "Job Position Count by Requisition": {
        "query": "SELECT COUNT(*) FROM job_req_positions WHERE job_req_id = :job_req_id",
        "required_params": ["job_req_id"],
        "result_type": "integer",
        "description": "Count position entries for a specific job requisition",
    },
    "Job Has Primary Position": {
        "query": "SELECT COUNT(*) FROM job_req_positions WHERE job_req_id = :job_req_id AND is_primary = 1",
        "required_params": ["job_req_id"],
        "result_type": "boolean",
        "description": "Check if job has a primary position assigned",
    },
    "Job Position Title": {
        "query": "SELECT position_title FROM job_req_positions WHERE job_req_id = :job_req_id AND is_primary = 1",
        "required_params": ["job_req_id"],
        "result_type": "string",
        "description": "Get primary position title for a job requisition",
    },
}


async def sap_recruiting_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any SAP Recruiting field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    # Extract verifier metadata
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    # Get field selection and strip arrow notation if present
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

    # Strip arrow notation (e.g., "User Exists by Username → Username" -> "User Exists by Username")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown field_to_check: {field_to_check}",
        )

    # Extract required parameters from verifier_values
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing for '{field_to_check}'",
            )
        params[param] = value

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No databases found in snapshot",
        )

    # Filter to SAP Recruiting database only
    # Expected alias: .apps_data_sap_recruiting_data (from /.apps_data/sap_recruiting/data.db)
    sap_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "sap_recruiting" in alias.lower()
    ]
    if len(sap_dbs) != 1:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Expected exactly 1 SAP Recruiting database (alias containing 'sap_recruiting'), found {len(sap_dbs)}. Available: {list(dbs.keys())}",
        )

    _alias, db_info = sap_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="SAP Recruiting database found but connection is None",
        )

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"SAP Recruiting field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Query execution failed: {e!s}",
        )

    # Compare based on result_type
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation (only needed for decimal comparisons)
        try:
            tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
            if tolerance_value is None:
                tolerance_value = 0.01
            tolerance = Decimal(str(tolerance_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid tolerance value: {e!s}",
            )

        # Ensure tolerance is non-negative
        if tolerance < 0:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Tolerance must be non-negative, got: {tolerance}",
            )

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: {float(result_decimal):.2f} {'✓' if matches else f'✗ expected {float(expected_decimal):.2f}'}"
    elif result_type == "boolean":
        # Convert various result types to boolean with robust handling
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in ("true", "1", "yes", "t", "y")
            else:
                # Try numeric comparison as fallback
                actual_bool = float(result) > 0
        except (TypeError, ValueError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid boolean value - result: {result}. Error: {e!s}",
            )
        # Use consistent truthy values for expected
        expected_str = str(expected_value).lower()
        expected_bool = expected_str in ("true", "1", "yes", "t", "y")
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            matches = int(result) == int(expected_value)
        except (ValueError, TypeError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    elif result_type == "string":
        # Case-insensitive string comparison with consistent None handling
        actual_str = str(result).strip().lower() if result is not None else ""
        expected_str = (
            str(expected_value).strip().lower() if expected_value is not None else ""
        )
        matches = actual_str == expected_str
        message = f"{field_to_check}: '{result}' {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
