\restrict dbmate

-- Dumped from database version 17.12 (Debian 17.12-1.pgdg13+1)
-- Dumped by pg_dump version 17.12 (Debian 17.12-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ballot_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ballot_items (
    id bigint NOT NULL,
    meeting_id bigint NOT NULL,
    number text NOT NULL,
    title text,
    type text,
    category text,
    effect text,
    lead_filer text,
    representative text,
    co_filers text[] DEFAULT '{}'::text[] NOT NULL,
    board_recommendation text,
    recommendation_quote text,
    vote_base text,
    vote_threshold_pct numeric,
    abstentions_count_against boolean,
    vote_standard_text text,
    vote_standard_quote text,
    request text,
    status text DEFAULT 'active'::text NOT NULL,
    source_quote text,
    extraction_run_id bigint NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT ballot_items_board_recommendation_check CHECK ((board_recommendation = ANY (ARRAY['for'::text, 'against'::text, 'abstain'::text, 'none'::text, 'for_each_nominee'::text, 'one_year'::text, 'two_years'::text, 'three_years'::text]))),
    CONSTRAINT ballot_items_category_check CHECK ((category = ANY (ARRAY['director_election'::text, 'say_on_pay'::text, 'say_on_pay_frequency'::text, 'auditor_ratification'::text, 'equity_plan'::text, 'charter_bylaw_amendment'::text, 'shareholder_proposal'::text, 'other'::text]))),
    CONSTRAINT ballot_items_effect_check CHECK ((effect = ANY (ARRAY['binding'::text, 'advisory'::text, 'precatory'::text]))),
    CONSTRAINT ballot_items_status_check CHECK ((status = ANY (ARRAY['active'::text, 'withdrawn'::text]))),
    CONSTRAINT ballot_items_type_check CHECK ((type = ANY (ARRAY['management'::text, 'shareholder'::text]))),
    CONSTRAINT ballot_items_vote_base_check CHECK ((vote_base = ANY (ARRAY['votes_cast'::text, 'shares_present_and_voting'::text, 'shares_present'::text, 'shares_outstanding'::text, 'plurality'::text, 'other'::text]))),
    CONSTRAINT ballot_items_vote_threshold_pct_check CHECK (((vote_threshold_pct > (0)::numeric) AND (vote_threshold_pct <= (100)::numeric)))
);


--
-- Name: ballot_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.ballot_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.ballot_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: check_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.check_results (
    id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    check_name text NOT NULL,
    passed boolean NOT NULL,
    detail text
);


--
-- Name: COLUMN check_results.check_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.check_results.check_name IS 'Spec §8.1 column `check`, renamed because CHECK is a reserved SQL word.';


--
-- Name: check_results_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.check_results ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.check_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: companies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.companies (
    id bigint NOT NULL,
    cik text NOT NULL,
    ticker text NOT NULL,
    name text NOT NULL,
    CONSTRAINT companies_cik_check CHECK ((cik ~ '^[0-9]{10}$'::text))
);


--
-- Name: companies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.companies ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.companies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: corrections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.corrections (
    id bigint NOT NULL,
    ballot_item_id bigint NOT NULL,
    field text NOT NULL,
    before jsonb,
    after jsonb,
    reason text NOT NULL,
    review_id bigint,
    corrected_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: corrections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.corrections ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.corrections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: extraction_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extraction_runs (
    id bigint NOT NULL,
    filing_id bigint NOT NULL,
    kind text NOT NULL,
    model text,
    effort text,
    prompt_version text,
    schema_version text,
    input_tokens bigint,
    output_tokens bigint,
    output jsonb,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT extraction_runs_input_tokens_check CHECK ((input_tokens >= 0)),
    CONSTRAINT extraction_runs_output_tokens_check CHECK ((output_tokens >= 0))
);


--
-- Name: extraction_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.extraction_runs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.extraction_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: filings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.filings (
    id bigint NOT NULL,
    accession text NOT NULL,
    company_id bigint NOT NULL,
    form text NOT NULL,
    filed_at timestamp with time zone NOT NULL,
    source_url text NOT NULL,
    primary_document text,
    raw_sha256 text,
    text_sha256 text,
    stage text DEFAULT 'discovered'::text NOT NULL,
    stage_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT filings_accession_check CHECK ((accession ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'::text)),
    CONSTRAINT filings_form_check CHECK ((form = ANY (ARRAY['DEF 14A'::text, 'DEFA14A'::text, '8-K'::text]))),
    CONSTRAINT filings_raw_sha256_check CHECK ((raw_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT filings_stage_check CHECK ((stage = ANY (ARRAY['discovered'::text, 'fetched'::text, 'extracted'::text, 'checked'::text, 'in_review'::text, 'published'::text, 'reconciled'::text]))),
    CONSTRAINT filings_text_sha256_check CHECK ((text_sha256 ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: filings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.filings ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.filings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: index_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.index_memberships (
    id bigint NOT NULL,
    company_id bigint NOT NULL,
    index_name text NOT NULL,
    start_date date NOT NULL,
    end_date date,
    CONSTRAINT index_memberships_check CHECK (((end_date IS NULL) OR (end_date >= start_date))),
    CONSTRAINT index_memberships_index_name_check CHECK ((index_name = ANY (ARRAY['sp100'::text, 'sp500'::text])))
);


--
-- Name: COLUMN index_memberships.index_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.index_memberships.index_name IS 'Spec §8.1 column `index`, renamed because INDEX is an SQL keyword.';


--
-- Name: index_memberships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.index_memberships ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.index_memberships_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: item_texts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.item_texts (
    id bigint NOT NULL,
    ballot_item_id bigint NOT NULL,
    kind text NOT NULL,
    version integer NOT NULL,
    body text,
    origin text NOT NULL,
    extraction_run_id bigint,
    review_status text DEFAULT 'pending'::text NOT NULL,
    CONSTRAINT item_texts_kind_check CHECK ((kind = ANY (ARRAY['summary'::text, 'proponent_argument'::text, 'board_argument'::text]))),
    CONSTRAINT item_texts_origin_check CHECK ((origin = ANY (ARRAY['model'::text, 'reviewer'::text]))),
    CONSTRAINT item_texts_review_status_check CHECK ((review_status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'needs_manual'::text]))),
    CONSTRAINT item_texts_version_check CHECK ((version > 0))
);


--
-- Name: item_texts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.item_texts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.item_texts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: meetings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meetings (
    id bigint NOT NULL,
    company_id bigint NOT NULL,
    proxy_filing_id bigint NOT NULL,
    kind text,
    meeting_date date,
    meeting_time text,
    format text,
    record_date date,
    shares_outstanding bigint,
    date_quote text,
    CONSTRAINT meetings_format_check CHECK ((format = ANY (ARRAY['virtual'::text, 'in_person'::text, 'hybrid'::text]))),
    CONSTRAINT meetings_kind_check CHECK ((kind = ANY (ARRAY['annual'::text, 'special'::text]))),
    CONSTRAINT meetings_shares_outstanding_check CHECK ((shares_outstanding >= 0))
);


--
-- Name: meetings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.meetings ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.meetings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nominees; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nominees (
    id bigint NOT NULL,
    ballot_item_id bigint NOT NULL,
    name text NOT NULL,
    "position" integer NOT NULL,
    CONSTRAINT nominees_position_check CHECK (("position" > 0))
);


--
-- Name: nominees_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.nominees ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.nominees_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pending_changes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_changes (
    id bigint NOT NULL,
    ballot_item_id bigint NOT NULL,
    field text NOT NULL,
    proposed jsonb,
    source_quote text,
    extraction_run_id bigint NOT NULL,
    reason text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    CONSTRAINT pending_changes_reason_check CHECK ((reason = ANY (ARRAY['rerun'::text, 'supplement'::text, 'reconciliation'::text]))),
    CONSTRAINT pending_changes_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'applied'::text, 'rejected'::text])))
);


--
-- Name: pending_changes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.pending_changes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.pending_changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: public_corrections; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.public_corrections AS
 SELECT co.id,
    co.ballot_item_id,
    bi.number,
    bi.title,
    c.ticker,
    m.meeting_date,
    co.field,
    co.before,
    co.after,
    co.reason,
    co.corrected_at
   FROM (((public.corrections co
     JOIN public.ballot_items bi ON ((bi.id = co.ballot_item_id)))
     JOIN public.meetings m ON ((m.id = bi.meeting_id)))
     JOIN public.companies c ON ((c.id = m.company_id)));


--
-- Name: published_ballot_items; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.published_ballot_items AS
 SELECT bi.id,
    bi.meeting_id,
    c.cik,
    c.ticker,
    c.name AS company_name,
    m.meeting_date,
    m.date_quote,
    f.source_url,
    bi.number,
    bi.title,
    bi.type,
    bi.category,
    bi.effect,
    bi.lead_filer,
    bi.representative,
    bi.co_filers,
    ARRAY( SELECT n.name
           FROM public.nominees n
          WHERE (n.ballot_item_id = bi.id)
          ORDER BY n."position") AS nominees,
    bi.board_recommendation,
    bi.recommendation_quote,
    bi.vote_base,
    bi.vote_threshold_pct,
    bi.abstentions_count_against,
    bi.vote_standard_text,
    bi.vote_standard_quote,
    bi.request,
    bi.status,
    bi.source_quote,
    bi.published_at,
    (EXISTS ( SELECT 1
           FROM public.pending_changes pc
          WHERE ((pc.ballot_item_id = bi.id) AND (pc.status = 'pending'::text)))) AS has_pending_change
   FROM (((public.ballot_items bi
     JOIN public.meetings m ON ((m.id = bi.meeting_id)))
     JOIN public.companies c ON ((c.id = m.company_id)))
     JOIN public.filings f ON ((f.id = m.proxy_filing_id)))
  WHERE (bi.published_at IS NOT NULL);


--
-- Name: published_item_texts; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.published_item_texts AS
 SELECT DISTINCT ON (t.ballot_item_id, t.kind) t.ballot_item_id,
    t.kind,
    t.version,
    t.body,
    t.origin
   FROM (public.item_texts t
     JOIN public.ballot_items bi ON ((bi.id = t.ballot_item_id)))
  WHERE ((t.review_status = 'approved'::text) AND (bi.published_at IS NOT NULL))
  ORDER BY t.ballot_item_id, t.kind, t.version DESC;


--
-- Name: published_meetings; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.published_meetings AS
 SELECT m.id,
    m.company_id,
    c.cik,
    c.ticker,
    c.name AS company_name,
    m.kind,
    m.meeting_date,
    m.meeting_time,
    m.format,
    m.record_date,
    m.shares_outstanding,
    m.date_quote,
    f.source_url
   FROM ((public.meetings m
     JOIN public.companies c ON ((c.id = m.company_id)))
     JOIN public.filings f ON ((f.id = m.proxy_filing_id)))
  WHERE (EXISTS ( SELECT 1
           FROM public.ballot_items bi
          WHERE ((bi.meeting_id = m.id) AND (bi.published_at IS NOT NULL))));


--
-- Name: vote_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vote_results (
    id bigint NOT NULL,
    ballot_item_id bigint NOT NULL,
    nominee_id bigint,
    votes_for bigint,
    votes_against bigint,
    abstentions bigint,
    broker_non_votes bigint,
    votes_one_year bigint,
    votes_two_years bigint,
    votes_three_years bigint,
    outcome text,
    source_filing_id bigint NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT vote_results_abstentions_check CHECK ((abstentions >= 0)),
    CONSTRAINT vote_results_broker_non_votes_check CHECK ((broker_non_votes >= 0)),
    CONSTRAINT vote_results_votes_against_check CHECK ((votes_against >= 0)),
    CONSTRAINT vote_results_votes_for_check CHECK ((votes_for >= 0)),
    CONSTRAINT vote_results_votes_one_year_check CHECK ((votes_one_year >= 0)),
    CONSTRAINT vote_results_votes_three_years_check CHECK ((votes_three_years >= 0)),
    CONSTRAINT vote_results_votes_two_years_check CHECK ((votes_two_years >= 0))
);


--
-- Name: published_vote_results; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.published_vote_results AS
 SELECT vr.id,
    vr.ballot_item_id,
    vr.nominee_id,
    n.name AS nominee_name,
    n."position" AS nominee_position,
    vr.votes_for,
    vr.votes_against,
    vr.abstentions,
    vr.broker_non_votes,
    vr.votes_one_year,
    vr.votes_two_years,
    vr.votes_three_years,
    vr.outcome,
    f.source_url,
    vr.published_at
   FROM (((public.vote_results vr
     JOIN public.ballot_items bi ON ((bi.id = vr.ballot_item_id)))
     JOIN public.filings f ON ((f.id = vr.source_filing_id)))
     LEFT JOIN public.nominees n ON ((n.id = vr.nominee_id)))
  WHERE ((vr.published_at IS NOT NULL) AND (bi.published_at IS NOT NULL));


--
-- Name: reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reviews (
    id bigint NOT NULL,
    filing_id bigint NOT NULL,
    item_text_id bigint,
    decision text NOT NULL,
    reason text,
    reviewer text NOT NULL,
    before jsonb,
    after jsonb,
    decided_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: reviews_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.reviews ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: vote_results_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vote_results ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vote_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ballot_items ballot_items_meeting_id_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ballot_items
    ADD CONSTRAINT ballot_items_meeting_id_number_key UNIQUE (meeting_id, number);


--
-- Name: ballot_items ballot_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ballot_items
    ADD CONSTRAINT ballot_items_pkey PRIMARY KEY (id);


--
-- Name: check_results check_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.check_results
    ADD CONSTRAINT check_results_pkey PRIMARY KEY (id);


--
-- Name: companies companies_cik_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_cik_key UNIQUE (cik);


--
-- Name: companies companies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_pkey PRIMARY KEY (id);


--
-- Name: corrections corrections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.corrections
    ADD CONSTRAINT corrections_pkey PRIMARY KEY (id);


--
-- Name: extraction_runs extraction_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_pkey PRIMARY KEY (id);


--
-- Name: filings filings_accession_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.filings
    ADD CONSTRAINT filings_accession_key UNIQUE (accession);


--
-- Name: filings filings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.filings
    ADD CONSTRAINT filings_pkey PRIMARY KEY (id);


--
-- Name: index_memberships index_memberships_company_id_index_name_start_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.index_memberships
    ADD CONSTRAINT index_memberships_company_id_index_name_start_date_key UNIQUE (company_id, index_name, start_date);


--
-- Name: index_memberships index_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.index_memberships
    ADD CONSTRAINT index_memberships_pkey PRIMARY KEY (id);


--
-- Name: item_texts item_texts_ballot_item_id_kind_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_texts
    ADD CONSTRAINT item_texts_ballot_item_id_kind_version_key UNIQUE (ballot_item_id, kind, version);


--
-- Name: item_texts item_texts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_texts
    ADD CONSTRAINT item_texts_pkey PRIMARY KEY (id);


--
-- Name: meetings meetings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_pkey PRIMARY KEY (id);


--
-- Name: nominees nominees_ballot_item_id_position_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nominees
    ADD CONSTRAINT nominees_ballot_item_id_position_key UNIQUE (ballot_item_id, "position");


--
-- Name: nominees nominees_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nominees
    ADD CONSTRAINT nominees_pkey PRIMARY KEY (id);


--
-- Name: pending_changes pending_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_changes
    ADD CONSTRAINT pending_changes_pkey PRIMARY KEY (id);


--
-- Name: reviews reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: vote_results vote_results_ballot_item_id_nominee_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vote_results
    ADD CONSTRAINT vote_results_ballot_item_id_nominee_id_key UNIQUE NULLS NOT DISTINCT (ballot_item_id, nominee_id);


--
-- Name: vote_results vote_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vote_results
    ADD CONSTRAINT vote_results_pkey PRIMARY KEY (id);


--
-- Name: ballot_items_extraction_run_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ballot_items_extraction_run_id_idx ON public.ballot_items USING btree (extraction_run_id);


--
-- Name: check_results_extraction_run_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX check_results_extraction_run_id_idx ON public.check_results USING btree (extraction_run_id);


--
-- Name: corrections_ballot_item_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX corrections_ballot_item_id_idx ON public.corrections USING btree (ballot_item_id);


--
-- Name: extraction_runs_filing_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX extraction_runs_filing_id_idx ON public.extraction_runs USING btree (filing_id);


--
-- Name: filings_company_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filings_company_id_idx ON public.filings USING btree (company_id);


--
-- Name: filings_stage_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filings_stage_idx ON public.filings USING btree (stage);


--
-- Name: meetings_company_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX meetings_company_id_idx ON public.meetings USING btree (company_id);


--
-- Name: meetings_proxy_filing_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX meetings_proxy_filing_id_idx ON public.meetings USING btree (proxy_filing_id);


--
-- Name: pending_changes_ballot_item_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pending_changes_ballot_item_id_idx ON public.pending_changes USING btree (ballot_item_id);


--
-- Name: reviews_filing_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reviews_filing_id_idx ON public.reviews USING btree (filing_id);


--
-- Name: ballot_items ballot_items_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ballot_items
    ADD CONSTRAINT ballot_items_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id) ON DELETE RESTRICT;


--
-- Name: ballot_items ballot_items_meeting_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ballot_items
    ADD CONSTRAINT ballot_items_meeting_id_fkey FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE RESTRICT;


--
-- Name: check_results check_results_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.check_results
    ADD CONSTRAINT check_results_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id) ON DELETE RESTRICT;


--
-- Name: corrections corrections_ballot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.corrections
    ADD CONSTRAINT corrections_ballot_item_id_fkey FOREIGN KEY (ballot_item_id) REFERENCES public.ballot_items(id) ON DELETE RESTRICT;


--
-- Name: corrections corrections_review_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.corrections
    ADD CONSTRAINT corrections_review_id_fkey FOREIGN KEY (review_id) REFERENCES public.reviews(id) ON DELETE RESTRICT;


--
-- Name: extraction_runs extraction_runs_filing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_filing_id_fkey FOREIGN KEY (filing_id) REFERENCES public.filings(id) ON DELETE RESTRICT;


--
-- Name: filings filings_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.filings
    ADD CONSTRAINT filings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE RESTRICT;


--
-- Name: index_memberships index_memberships_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.index_memberships
    ADD CONSTRAINT index_memberships_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE RESTRICT;


--
-- Name: item_texts item_texts_ballot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_texts
    ADD CONSTRAINT item_texts_ballot_item_id_fkey FOREIGN KEY (ballot_item_id) REFERENCES public.ballot_items(id) ON DELETE RESTRICT;


--
-- Name: item_texts item_texts_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_texts
    ADD CONSTRAINT item_texts_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id) ON DELETE RESTRICT;


--
-- Name: meetings meetings_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE RESTRICT;


--
-- Name: meetings meetings_proxy_filing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_proxy_filing_id_fkey FOREIGN KEY (proxy_filing_id) REFERENCES public.filings(id) ON DELETE RESTRICT;


--
-- Name: nominees nominees_ballot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nominees
    ADD CONSTRAINT nominees_ballot_item_id_fkey FOREIGN KEY (ballot_item_id) REFERENCES public.ballot_items(id) ON DELETE RESTRICT;


--
-- Name: pending_changes pending_changes_ballot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_changes
    ADD CONSTRAINT pending_changes_ballot_item_id_fkey FOREIGN KEY (ballot_item_id) REFERENCES public.ballot_items(id) ON DELETE RESTRICT;


--
-- Name: pending_changes pending_changes_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_changes
    ADD CONSTRAINT pending_changes_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id) ON DELETE RESTRICT;


--
-- Name: reviews reviews_filing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_filing_id_fkey FOREIGN KEY (filing_id) REFERENCES public.filings(id) ON DELETE RESTRICT;


--
-- Name: reviews reviews_item_text_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_item_text_id_fkey FOREIGN KEY (item_text_id) REFERENCES public.item_texts(id) ON DELETE RESTRICT;


--
-- Name: vote_results vote_results_ballot_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vote_results
    ADD CONSTRAINT vote_results_ballot_item_id_fkey FOREIGN KEY (ballot_item_id) REFERENCES public.ballot_items(id) ON DELETE RESTRICT;


--
-- Name: vote_results vote_results_nominee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vote_results
    ADD CONSTRAINT vote_results_nominee_id_fkey FOREIGN KEY (nominee_id) REFERENCES public.nominees(id) ON DELETE RESTRICT;


--
-- Name: vote_results vote_results_source_filing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vote_results
    ADD CONSTRAINT vote_results_source_filing_id_fkey FOREIGN KEY (source_filing_id) REFERENCES public.filings(id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261121000001'),
    ('20261121000002'),
    ('20261121000003');
