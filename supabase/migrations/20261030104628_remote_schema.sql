SET local check_function_bodies = off;

CREATE TABLE "public"."claim_documents" (
  "doc_id"    uuid                   NOT NULL DEFAULT extensions.uuid_generate_v4(),
  "case_id"   uuid,
  "file_name" character varying(255) NOT NULL,
  CONSTRAINT "claim_documents_pkey" PRIMARY KEY (doc_id)
);

CREATE TABLE "public"."claims_cases" (
  "case_id"                      uuid                     NOT NULL DEFAULT extensions.uuid_generate_v4(),
  "case_number"                  character varying(50)    NOT NULL,
  "package_code"                 character varying(50),
  "primary_diagnosis"            character varying(255),
  "requested_procedure"          character varying(255),
  "missing_mandatory_docs"       text[]                   DEFAULT '{}'::text[],
  "admissibility_score"          numeric(5,2),
  "case_status"                  character varying(50)    DEFAULT 'DRAFT'::character varying,
  "patient_pseudo_id"            character varying(100),
  "patient_gender"               character varying(20),
  "hospital_reg_no"              character varying(100),
  "doctor_reg_no"                character varying(100),
  "admission_date"               date,
  "discharge_date"               date,
  "claimed_amount"               numeric(12,2),
  "fraud_risk_score"             numeric(5,2)             DEFAULT 0.00,
  "fraud_flags"                  jsonb                    DEFAULT '[]'::jsonb,
  "created_at"                   timestamp with time zone DEFAULT now(),
  "updated_at"                   timestamp with time zone DEFAULT now(),
  "pipeline_stage"               character varying(50)    DEFAULT 'PENDING_OCR'::character varying,
  "total_documents"              integer                  DEFAULT 0,
  "case_audit_summary"           jsonb                    DEFAULT '{}'::jsonb,
  "master_ocr_bundle_json"       jsonb,
  "sanitized_master_bundle_json" jsonb,
  "pii_audit_summary"            jsonb,
  "is_master_pii_purged"         boolean                  DEFAULT false,
  "master_fhir_bundle_json"      jsonb,
  CONSTRAINT "claims_cases_case_number_key" UNIQUE (case_number),
  CONSTRAINT "claims_cases_pkey" PRIMARY KEY (case_id)
);

CREATE TYPE "public"."doc_category" AS ENUM (
  'DISCHARGE_SUMMARY',
  'LAB_REPORT',
  'PRESCRIPTION',
  'RADIOLOGY_MRI',
  'CLINICAL_NOTES',
  'OTHER'
);

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "document_type" public.doc_category DEFAULT 'OTHER'::public.doc_category;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "raw_storage_path" text NOT NULL;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "preview_storage_paths" text[] DEFAULT '{}'::text[];

CREATE TYPE "public"."doc_status" AS ENUM (
  'PENDING_OCR',
  'OCR_PROCESSING',
  'RFI_BLURRY_SCAN',
  'PENDING_PII',
  'PENDING_FHIR',
  'COMPLETED',
  'FAILED'
);

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "processing_status" public.doc_status DEFAULT 'PENDING_OCR'::public.doc_status;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "avg_ocr_confidence" numeric(5,4);

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "low_conf_line_count" integer DEFAULT 0;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "is_confidence_acceptable" boolean DEFAULT true;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "rfi_reason" text;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "raw_ocr_json" jsonb;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "redacted_ocr_json" jsonb;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "pii_redaction_audit" jsonb;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "is_raw_pii_purged" boolean DEFAULT false;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "fhir_bundle_json" jsonb;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "extracted_clinical_facts" jsonb;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "uploaded_at" timestamp WITH time zone DEFAULT now();

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "ocr_completed_at" timestamp WITH time zone;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "pii_completed_at" timestamp WITH time zone;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "fhir_completed_at" timestamp WITH time zone;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "source_size_bytes" bigint;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "document_status" character varying(50) DEFAULT 'PENDING'::character varying;

ALTER TABLE "public"."claim_documents"
  ADD COLUMN "audit_summary" jsonb DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION public.purge_master_case_pii()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  AS $function$
BEGIN
    IF NEW.pipeline_stage = 'PENDING_FHIR' AND NEW.sanitized_master_bundle_json IS NOT NULL THEN
        NEW.master_ocr_bundle_json := NULL;
        NEW.is_master_pii_purged := TRUE;
        NEW.updated_at := NOW();
        -- Also wipe individual raw_ocr_json in child documents for this case
        UPDATE claim_documents SET raw_ocr_json = NULL, is_raw_pii_purged = TRUE WHERE case_id = NEW.case_id;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.purge_raw_pii_on_redaction()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  AS $function$
BEGIN
    IF NEW.processing_status = 'PENDING_FHIR' AND NEW.redacted_ocr_json IS NOT NULL THEN
        NEW.raw_ocr_json := NULL;
        NEW.is_raw_pii_purged := TRUE;
        NEW.pii_completed_at := NOW();
    END IF;
    RETURN NEW;
END;
$function$;

ALTER TABLE "public"."claim_documents"
  ADD CONSTRAINT "claim_documents_case_id_fkey" FOREIGN KEY (case_id) REFERENCES public.claims_cases(case_id) ON DELETE CASCADE;

CREATE INDEX idx_cases_doctor_dates ON public.claims_cases USING btree (doctor_reg_no, admission_date, discharge_date);

CREATE INDEX idx_cases_package_amount ON public.claims_cases USING btree (package_code, claimed_amount);

CREATE INDEX idx_claim_docs_case_id ON public.claim_documents USING btree (case_id);

CREATE INDEX idx_claim_docs_status ON public.claim_documents USING btree (processing_status);

CREATE TRIGGER trg_purge_raw_pii
  BEFORE UPDATE ON public.claim_documents
  FOR EACH ROW
  EXECUTE FUNCTION public.purge_raw_pii_on_redaction();

CREATE TRIGGER trg_purge_master_case_pii
  BEFORE UPDATE ON public.claims_cases
  FOR EACH ROW
  EXECUTE FUNCTION public.purge_master_case_pii();

CREATE POLICY "Allow MVP Sprint Access to Raw Docs" ON "storage"."objects"
  FOR ALL
  TO PUBLIC
  USING ((bucket_id = ANY (ARRAY['raw-medical-docs'::text, 'processed-previews'::text])))
  WITH CHECK ((bucket_id = ANY (ARRAY['raw-medical-docs'::text, 'processed-previews'::text])));

GRANT EXECUTE ON FUNCTION "public"."purge_master_case_pii"() TO PUBLIC, "anon", "authenticated";

REVOKE ALL ON FUNCTION "public"."purge_master_case_pii"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."purge_master_case_pii"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."purge_master_case_pii"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."purge_raw_pii_on_redaction"() TO PUBLIC, "anon", "authenticated";

REVOKE ALL ON FUNCTION "public"."purge_raw_pii_on_redaction"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."purge_raw_pii_on_redaction"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."purge_raw_pii_on_redaction"() TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claim_documents" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."claim_documents" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claim_documents" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claim_documents" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claims_cases" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."claims_cases" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claims_cases" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."claims_cases" TO "service_role";

