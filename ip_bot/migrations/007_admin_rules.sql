ALTER TABLE knowledge_revisions DROP CONSTRAINT knowledge_revisions_kind_check;
ALTER TABLE knowledge_revisions ADD CONSTRAINT knowledge_revisions_kind_check CHECK (kind IN ('faq', 'news', 'tax_rules', 'activities'));
ALTER TABLE knowledge_publications DROP CONSTRAINT knowledge_publications_kind_check;
ALTER TABLE knowledge_publications ADD CONSTRAINT knowledge_publications_kind_check CHECK (kind IN ('faq', 'news', 'tax_rules', 'activities'));
