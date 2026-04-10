-- Greenhouse MCP Server Database Schema
-- This file contains the raw DDL statements for reference.
-- The actual schema is managed via SQLAlchemy ORM models in db/models/
--
-- API Reference: Greenhouse Harvest API and Job Board API
-- All field names follow snake_case convention matching the API responses.

-- ============================================================================
-- USERS & ORGANIZATION
-- ============================================================================

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    primary_email_address TEXT UNIQUE NOT NULL,
    employee_id TEXT,
    disabled BOOLEAN DEFAULT FALSE,
    site_admin BOOLEAN DEFAULT FALSE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE user_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL
);

CREATE TABLE departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES departments(id),
    external_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE offices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location_name TEXT,
    parent_id INTEGER REFERENCES offices(id),
    primary_contact_user_id INTEGER REFERENCES users(id),
    external_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE user_departments (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, department_id)
);

CREATE TABLE user_offices (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    office_id INTEGER REFERENCES offices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, office_id)
);

-- ============================================================================
-- SOURCES
-- ============================================================================

CREATE TABLE source_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type_id INTEGER REFERENCES source_types(id)
);

-- ============================================================================
-- JOBS & PIPELINE
-- ============================================================================

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    requisition_id TEXT,
    notes TEXT,
    confidential BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'draft',
    opened_at TEXT,
    closed_at TEXT,
    is_template BOOLEAN DEFAULT FALSE,
    copied_from_id INTEGER REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_departments (
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, department_id)
);

CREATE TABLE job_offices (
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    office_id INTEGER REFERENCES offices(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, office_id)
);

CREATE TABLE hiring_team (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    responsible BOOLEAN DEFAULT FALSE,
    created_at TEXT
);

CREATE TABLE job_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE interview_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_stage_id INTEGER NOT NULL REFERENCES job_stages(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    schedulable BOOLEAN DEFAULT TRUE,
    estimated_minutes INTEGER DEFAULT 30,
    interview_kit_id INTEGER,
    interview_kit_content TEXT,
    created_at TEXT
);

CREATE TABLE interview_kit_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_step_id INTEGER NOT NULL REFERENCES interview_steps(id) ON DELETE CASCADE,
    question TEXT NOT NULL
);

CREATE TABLE interview_step_default_interviewers (
    interview_step_id INTEGER REFERENCES interview_steps(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (interview_step_id, user_id)
);

-- ============================================================================
-- JOB POSTS (Job Board API)
-- ============================================================================

CREATE TABLE job_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    location_name TEXT,
    content TEXT,
    absolute_url TEXT,
    language TEXT DEFAULT 'en',
    internal BOOLEAN DEFAULT FALSE,
    live BOOLEAN DEFAULT FALSE,
    first_published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_post_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_post_id INTEGER NOT NULL REFERENCES job_posts(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    required BOOLEAN DEFAULT FALSE,
    field_name TEXT NOT NULL,
    field_type TEXT NOT NULL
);

CREATE TABLE job_post_question_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES job_post_questions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    value INTEGER NOT NULL
);

-- ============================================================================
-- PROSPECT POOLS
-- ============================================================================

CREATE TABLE prospect_pools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE prospect_pool_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_pool_id INTEGER NOT NULL REFERENCES prospect_pools(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    priority INTEGER DEFAULT 0
);

-- ============================================================================
-- JOB OPENINGS
-- ============================================================================

CREATE TABLE job_openings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    opening_id TEXT,
    status TEXT DEFAULT 'open',
    opened_at TEXT,
    closed_at TEXT,
    application_id INTEGER REFERENCES applications(id),
    close_reason_id INTEGER,
    close_reason_name TEXT,
    created_at TEXT
);

-- ============================================================================
-- CANDIDATES
-- ============================================================================

CREATE TABLE candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    company TEXT,
    title TEXT,
    photo_url TEXT,
    is_private BOOLEAN DEFAULT FALSE,
    can_email BOOLEAN DEFAULT TRUE,
    recruiter_id INTEGER REFERENCES users(id),
    coordinator_id INTEGER REFERENCES users(id),
    last_activity TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE candidate_phone_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT DEFAULT 'mobile'
);

CREATE TABLE candidate_email_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT DEFAULT 'personal'
);

CREATE TABLE candidate_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT DEFAULT 'home'
);

CREATE TABLE candidate_website_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT DEFAULT 'personal'
);

CREATE TABLE candidate_social_media_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value TEXT NOT NULL
);

CREATE TABLE candidate_educations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    school_name TEXT,
    degree TEXT,
    discipline TEXT,
    start_date TEXT,
    end_date TEXT
);

CREATE TABLE candidate_employments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    company_name TEXT,
    title TEXT,
    start_date TEXT,
    end_date TEXT
);

CREATE TABLE candidate_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    url TEXT,
    type TEXT,
    created_at TEXT
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE candidate_tags (
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (candidate_id, tag_id)
);

-- ============================================================================
-- APPLICATIONS
-- ============================================================================

CREATE TABLE rejection_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type_id INTEGER,
    type_name TEXT
);

CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id INTEGER REFERENCES jobs(id),
    prospect BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'active',
    current_stage_id INTEGER REFERENCES job_stages(id),
    source_id INTEGER REFERENCES sources(id),
    credited_to_id INTEGER REFERENCES users(id),
    recruiter_id INTEGER REFERENCES users(id),
    coordinator_id INTEGER REFERENCES users(id),
    rejection_reason_id INTEGER REFERENCES rejection_reasons(id),
    job_post_id INTEGER REFERENCES job_posts(id),
    location_address TEXT,
    prospect_pool_id INTEGER REFERENCES prospect_pools(id),
    prospect_stage_id INTEGER REFERENCES prospect_pool_stages(id),
    prospect_owner_id INTEGER REFERENCES users(id),
    prospective_office_id INTEGER REFERENCES offices(id),
    prospective_department_id INTEGER REFERENCES departments(id),
    applied_at TEXT,
    rejected_at TEXT,
    last_activity_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE application_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT
);

-- ============================================================================
-- SCORECARDS (Interview Feedback)
-- ============================================================================

CREATE TABLE scorecards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    interview_step_id INTEGER REFERENCES interview_steps(id),
    interview_name TEXT,
    interviewer_id INTEGER REFERENCES users(id),
    submitted_by_id INTEGER REFERENCES users(id),
    overall_recommendation TEXT,
    interviewed_at TEXT,
    submitted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE scorecard_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scorecard_id INTEGER NOT NULL REFERENCES scorecards(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'Skills',
    rating TEXT,
    note TEXT
);

CREATE TABLE scorecard_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scorecard_id INTEGER NOT NULL REFERENCES scorecards(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT
);

-- ============================================================================
-- ACTIVITY FEED
-- ============================================================================

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    body TEXT NOT NULL,
    visibility TEXT DEFAULT 'public',
    created_at TEXT
);

CREATE TABLE emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    subject TEXT,
    body TEXT,
    to_address TEXT,
    from_address TEXT,
    cc_address TEXT,
    created_at TEXT
);

CREATE TABLE activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    subject TEXT NOT NULL,
    body TEXT,
    created_at TEXT
);

-- ============================================================================
-- EDUCATION REFERENCE DATA (Job Board API)
-- ============================================================================

CREATE TABLE degrees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL
);

CREATE TABLE disciplines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL
);

CREATE TABLE schools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL
);

-- ============================================================================
-- INDEXES
-- ============================================================================

CREATE INDEX idx_users_email ON users(primary_email_address);
CREATE INDEX idx_user_emails_user ON user_emails(user_id);
CREATE INDEX idx_departments_parent ON departments(parent_id);
CREATE INDEX idx_offices_parent ON offices(parent_id);

CREATE INDEX idx_candidates_first_name ON candidates(first_name);
CREATE INDEX idx_candidates_last_name ON candidates(last_name);
CREATE INDEX idx_candidates_recruiter ON candidates(recruiter_id);
CREATE INDEX idx_candidate_emails_value ON candidate_email_addresses(value);
CREATE INDEX idx_candidate_emails_candidate ON candidate_email_addresses(candidate_id);
CREATE INDEX idx_candidate_tags_candidate ON candidate_tags(candidate_id);
CREATE INDEX idx_candidate_attachments_candidate ON candidate_attachments(candidate_id);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_job_stages_job ON job_stages(job_id);
CREATE INDEX idx_interview_steps_stage ON interview_steps(job_stage_id);
CREATE INDEX idx_interview_kit_questions_step ON interview_kit_questions(interview_step_id);
CREATE INDEX idx_hiring_team_job ON hiring_team(job_id);
CREATE INDEX idx_hiring_team_user ON hiring_team(user_id);
CREATE INDEX idx_job_openings_job ON job_openings(job_id);
CREATE INDEX idx_job_posts_job ON job_posts(job_id);
CREATE INDEX idx_job_posts_live ON job_posts(live, internal);

CREATE INDEX idx_applications_candidate ON applications(candidate_id);
CREATE INDEX idx_applications_job ON applications(job_id);
CREATE INDEX idx_applications_status ON applications(status);
CREATE INDEX idx_applications_stage ON applications(current_stage_id);
CREATE INDEX idx_application_answers_app ON application_answers(application_id);

CREATE INDEX idx_scorecards_application ON scorecards(application_id);
CREATE INDEX idx_scorecards_candidate ON scorecards(candidate_id);
CREATE INDEX idx_scorecard_attributes_scorecard ON scorecard_attributes(scorecard_id);
CREATE INDEX idx_scorecard_questions_scorecard ON scorecard_questions(scorecard_id);

CREATE INDEX idx_activities_candidate ON activities(candidate_id);
CREATE INDEX idx_notes_candidate ON notes(candidate_id);
CREATE INDEX idx_emails_candidate ON emails(candidate_id);
