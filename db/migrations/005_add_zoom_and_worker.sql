ALTER TABLE render_jobs
    DROP CONSTRAINT IF EXISTS render_jobs_style_check,
    ADD CONSTRAINT render_jobs_style_check
        CHECK (style IN ('mandelbrot', 'seahorse', 'skyline', 'geometric')),
    ADD COLUMN IF NOT EXISTS viewport JSONB,
    ADD COLUMN IF NOT EXISTS worker_name TEXT;
