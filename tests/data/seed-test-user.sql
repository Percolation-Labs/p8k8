-- Seed data for default test user: Sage Whitfield
-- user_id: 7d31eddf-7ff7-542a-982f-7522e7a3ec67
-- Run against any p8 database to populate feed with reminders + news

-- ============================================================
-- REMINDERS (moments with moment_type='reminder')
-- ============================================================

-- Today - 3 reminders
INSERT INTO moments (name, moment_type, summary, starts_timestamp, topic_tags, user_id, metadata, created_at) VALUES
(
  'morning-bird-count',
  'reminder',
  'Log morning bird count at the backyard feeder station',
  (CURRENT_DATE + TIME '07:30:00') AT TIME ZONE 'US/Pacific',
  ARRAY['birdwatching', 'daily'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "30 7 * * *", "recurrence": "recurring", "job_name": "reminder-bird-count"}'::jsonb,
  (CURRENT_DATE + TIME '06:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'water-seedlings',
  'reminder',
  'Water the Douglas fir seedlings in the cold frame',
  (CURRENT_DATE + TIME '10:00:00') AT TIME ZONE 'US/Pacific',
  ARRAY['permaculture', 'garden'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "0 10 * * *", "recurrence": "recurring", "job_name": "reminder-seedlings"}'::jsonb,
  (CURRENT_DATE + TIME '06:30:00') AT TIME ZONE 'US/Pacific'
),
(
  'trail-volunteer-signup',
  'reminder',
  'Sign up for Eagle Creek trail restoration volunteer day',
  (CURRENT_DATE + TIME '14:00:00') AT TIME ZONE 'US/Pacific',
  ARRAY['trails', 'volunteering'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "once", "recurrence": "once", "job_name": "reminder-trail-volunteer"}'::jsonb,
  (CURRENT_DATE + TIME '07:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;

-- Yesterday - 2 reminders
INSERT INTO moments (name, moment_type, summary, starts_timestamp, topic_tags, user_id, metadata, created_at) VALUES
(
  'check-owl-nest-cam',
  'reminder',
  'Check the barn owl nest cam footage from last night',
  (CURRENT_DATE - 1 + TIME '08:00:00') AT TIME ZONE 'US/Pacific',
  ARRAY['birdwatching', 'wildlife'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "0 8 * * *", "recurrence": "recurring", "job_name": "reminder-owl-cam"}'::jsonb,
  (CURRENT_DATE - 1 + TIME '07:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'submit-mushroom-id',
  'reminder',
  'Submit chanterelle photos to iNaturalist for species verification',
  (CURRENT_DATE - 1 + TIME '18:00:00') AT TIME ZONE 'US/Pacific',
  ARRAY['mushroom foraging', 'citizen-science'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "once", "recurrence": "once", "job_name": "reminder-mushroom-id"}'::jsonb,
  (CURRENT_DATE - 1 + TIME '09:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;

-- 2 days ago - 1 reminder
INSERT INTO moments (name, moment_type, summary, starts_timestamp, topic_tags, user_id, metadata, created_at) VALUES
(
  'cedar-vet-appointment',
  'reminder',
  'Take Cedar to the vet for annual checkup at 2pm',
  (CURRENT_DATE - 2 + TIME '14:00:00') AT TIME ZONE 'US/Pacific',
  ARRAY['cedar', 'health'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"schedule": "once", "recurrence": "once", "job_name": "reminder-cedar-vet"}'::jsonb,
  (CURRENT_DATE - 2 + TIME '08:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;

-- ============================================================
-- NEWS RESOURCES
-- ============================================================

-- Today - 3 news + 1 research
INSERT INTO resources (name, uri, content, category, image_uri, tags, user_id, metadata, created_at) VALUES
(
  'Spotted Owl Recovery Shows Promise in Old-Growth Forests',
  'https://www.audubon.org/news/spotted-owl-recovery-2026',
  'New survey data from the US Fish and Wildlife Service shows a 12% increase in northern spotted owl populations across Oregon and Washington old-growth forests. Conservationists credit habitat protection measures enacted in 2023.',
  'news',
  'https://images.unsplash.com/photo-1543549790-8b5f4a028cfb?w=600',
  ARRAY['birdwatching', 'conservation', 'pacific-northwest'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Audubon News", "published_date": "today"}'::jsonb,
  (CURRENT_DATE + TIME '08:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'Pacific Northwest Braces for Early Wildfire Season',
  'https://www.fs.usda.gov/news/pacific-nw-wildfire-2026',
  'The US Forest Service warns that below-average snowpack and dry conditions could lead to an early start to wildfire season in Oregon and Washington. Trail closures may begin as early as May.',
  'news',
  'https://images.unsplash.com/photo-1473448912268-2022ce9509d8?w=600',
  ARRAY['forest', 'wildfire', 'pacific-northwest', 'trails'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "US Forest Service", "published_date": "today"}'::jsonb,
  (CURRENT_DATE + TIME '09:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'Rare Chanterelle Variant Discovered in Columbia River Gorge',
  'https://www.inaturalist.org/observations/gorge-chanterelle-2026',
  'Mycologists confirm a new golden chanterelle variant found near Eagle Creek. The specimen shows unique gill patterns not previously documented in Pacific Northwest fungi databases.',
  'news',
  'https://images.unsplash.com/photo-1504198070170-4ca53bb1c1fa?w=600',
  ARRAY['mushroom foraging', 'mycology', 'columbia-river-gorge'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "iNaturalist", "published_date": "today"}'::jsonb,
  (CURRENT_DATE + TIME '10:30:00') AT TIME ZONE 'US/Pacific'
),
(
  'Forest Bathing and Cortisol: A Meta-Analysis of 47 Studies',
  'https://doi.org/10.1038/s41598-026-forest-bathing',
  'Comprehensive meta-analysis confirms significant cortisol reduction from regular forest exposure. Researchers found 2+ hours per week in old-growth forests produced the strongest effects on stress biomarkers.',
  'research',
  NULL,
  ARRAY['forest ecology', 'health', 'research'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Nature Scientific Reports", "doi": "10.1038/s41598-026-forest-bathing"}'::jsonb,
  (CURRENT_DATE + TIME '11:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;

-- Yesterday - 3 news
INSERT INTO resources (name, uri, content, category, image_uri, tags, user_id, metadata, created_at) VALUES
(
  'Great Backyard Bird Count 2026 Results: Record Participation',
  'https://www.audubon.org/news/gbbc-2026-results',
  'The 2026 Great Backyard Bird Count recorded over 400 million bird observations from 250,000 participants worldwide. Pacific Northwest birders reported notable increases in Anna''s Hummingbird sightings.',
  'news',
  'https://images.unsplash.com/photo-1552728089-57bdde30beb3?w=600',
  ARRAY['birdwatching', 'citizen-science'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Audubon News"}'::jsonb,
  (CURRENT_DATE - 1 + TIME '07:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'Oregon Passes Landmark Old-Growth Protection Bill',
  'https://www.treehugger.com/oregon-old-growth-bill-2026',
  'Oregon legislature passes comprehensive old-growth forest protection legislation, permanently safeguarding 500,000 acres of ancient Douglas fir, western red cedar, and Sitka spruce stands.',
  'news',
  'https://images.unsplash.com/photo-1448375240586-882707db888b?w=600',
  ARRAY['forest ecology', 'conservation', 'legislation'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Treehugger"}'::jsonb,
  (CURRENT_DATE - 1 + TIME '09:30:00') AT TIME ZONE 'US/Pacific'
),
(
  'Trail Conditions Update: Columbia River Gorge',
  'https://www.fs.usda.gov/gorge-trail-conditions-2026',
  'Eagle Creek Trail open to Punchbowl Falls. Oneonta Gorge remains closed for rockfall mitigation. Multnomah Falls loop trail fully accessible. Ice possible above 3000ft on all Gorge trails.',
  'news',
  'https://images.unsplash.com/photo-1441974231531-c6227db76b6e?w=600',
  ARRAY['trails', 'columbia-river-gorge', 'hiking'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "US Forest Service"}'::jsonb,
  (CURRENT_DATE - 1 + TIME '12:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;

-- 2 days ago - 1 news + 1 research
INSERT INTO resources (name, uri, content, category, image_uri, tags, user_id, metadata, created_at) VALUES
(
  'Permaculture Food Forest Yields First Winter Harvest in Portland',
  'https://www.treehugger.com/portland-food-forest-harvest-2026',
  'A 5-acre community permaculture food forest in SE Portland produced its first significant winter harvest, including cold-hardy kiwi, persimmons, and over 200 pounds of winter greens.',
  'news',
  'https://images.unsplash.com/photo-1416879595882-3373a0480b5b?w=600',
  ARRAY['permaculture', 'urban-farming', 'portland'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Treehugger"}'::jsonb,
  (CURRENT_DATE - 2 + TIME '08:00:00') AT TIME ZONE 'US/Pacific'
),
(
  'Acoustic Monitoring Reveals Hidden Biodiversity in Pacific Temperate Rainforests',
  'https://doi.org/10.1111/ecol.2026-acoustic-monitoring',
  'Researchers deployed autonomous recording units across 30 sites in the Hoh and Quinault rainforests. Analysis of 50,000 hours of audio identified 12 previously undocumented amphibian vocalizations.',
  'research',
  NULL,
  ARRAY['field recording', 'biodiversity', 'research', 'rainforest'],
  '7d31eddf-7ff7-542a-982f-7522e7a3ec67',
  '{"source": "Ecology Letters", "doi": "10.1111/ecol.2026-acoustic-monitoring"}'::jsonb,
  (CURRENT_DATE - 2 + TIME '10:00:00') AT TIME ZONE 'US/Pacific'
)
ON CONFLICT DO NOTHING;
