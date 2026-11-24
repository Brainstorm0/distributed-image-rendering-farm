ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS style TEXT NOT NULL
    DEFAULT 'mandelbrot' CHECK (style IN ('mandelbrot', 'seahorse', 'skyline'));
