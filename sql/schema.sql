-- CreateSchema
CREATE SCHEMA IF NOT EXISTS "public";

-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- CreateTable
CREATE TABLE "recipes" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_url" TEXT,
    "title" TEXT,
    "cuisine" TEXT,
    "est_time_minutes" INTEGER,
    "steps" JSONB,
    "raw_caption" TEXT,
    "servings" INTEGER NOT NULL DEFAULT 2,
    "extraction_status" TEXT NOT NULL DEFAULT 'pending',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "recipes_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "recipe_ingredients" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "recipe_id" UUID NOT NULL,
    "name" TEXT NOT NULL,
    "quantity" DECIMAL(10,2),
    "unit" TEXT,
    "qualitative_note" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "recipe_ingredients_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "pantry" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "ingredient_name" TEXT NOT NULL,
    "quantity" DECIMAL(10,2) NOT NULL,
    "unit" TEXT NOT NULL,
    "expiry_date" DATE,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "pantry_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "cook_slots" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "week_start_date" DATE NOT NULL,
    "slot_start" TIMESTAMPTZ NOT NULL,
    "slot_end" TIMESTAMPTZ NOT NULL,
    "duration_minutes" INTEGER NOT NULL,
    "suitability_score" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "assigned" BOOLEAN NOT NULL DEFAULT false,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "cook_slots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "meal_plan" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "recipe_id" UUID NOT NULL,
    "cook_slot_id" UUID,
    "week_start_date" DATE NOT NULL,
    "planned_date" DATE,
    "planned_start_time" TIMESTAMPTZ NOT NULL,
    "planned_end_time" TIMESTAMPTZ NOT NULL,
    "score" DOUBLE PRECISION,
    "score_reason" TEXT,
    "calendar_event_id" TEXT,
    "status" TEXT NOT NULL DEFAULT 'planned',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "meal_plan_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "shopping_list" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "week_start_date" DATE NOT NULL,
    "ingredient_name" TEXT NOT NULL,
    "quantity_needed" DECIMAL(10,2) NOT NULL,
    "unit" TEXT NOT NULL,
    "resolution_status" TEXT NOT NULL DEFAULT 'pending',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "shopping_list_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "instacart_orders" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "week_start_date" DATE NOT NULL,
    "cart_url" TEXT,
    "item_count" INTEGER NOT NULL DEFAULT 0,
    "unresolved_item_count" INTEGER NOT NULL DEFAULT 0,
    "method" TEXT,
    "delivery_window_start" TIMESTAMPTZ,
    "delivery_window_end" TIMESTAMPTZ,
    "delivery_event_id" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "instacart_orders_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "eval_log" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "stage" TEXT NOT NULL,
    "input_ref" TEXT,
    "success" BOOLEAN NOT NULL,
    "retry_count" INTEGER NOT NULL DEFAULT 0,
    "duration_ms" INTEGER,
    "error_message" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "eval_log_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "recipe_ingredients_recipe_id_idx" ON "recipe_ingredients"("recipe_id");

-- CreateIndex
CREATE INDEX "recipe_ingredients_name_idx" ON "recipe_ingredients"("name");

-- CreateIndex
CREATE INDEX "pantry_ingredient_name_idx" ON "pantry"("ingredient_name");

-- CreateIndex
CREATE INDEX "cook_slots_week_start_date_slot_start_idx" ON "cook_slots"("week_start_date", "slot_start");

-- CreateIndex
CREATE INDEX "meal_plan_week_start_date_idx" ON "meal_plan"("week_start_date");

-- CreateIndex
CREATE UNIQUE INDEX "meal_plan_week_start_date_recipe_id_key" ON "meal_plan"("week_start_date", "recipe_id");

-- CreateIndex
CREATE INDEX "shopping_list_week_start_date_idx" ON "shopping_list"("week_start_date");

-- CreateIndex
CREATE UNIQUE INDEX "shopping_list_week_start_date_ingredient_name_unit_key" ON "shopping_list"("week_start_date", "ingredient_name", "unit");

-- CreateIndex
CREATE UNIQUE INDEX "instacart_orders_week_start_date_key" ON "instacart_orders"("week_start_date");

-- CreateIndex
CREATE INDEX "eval_log_stage_created_at_idx" ON "eval_log"("stage", "created_at");

-- AddForeignKey
ALTER TABLE "recipe_ingredients" ADD CONSTRAINT "recipe_ingredients_recipe_id_fkey" FOREIGN KEY ("recipe_id") REFERENCES "recipes"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "meal_plan" ADD CONSTRAINT "meal_plan_recipe_id_fkey" FOREIGN KEY ("recipe_id") REFERENCES "recipes"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "meal_plan" ADD CONSTRAINT "meal_plan_cook_slot_id_fkey" FOREIGN KEY ("cook_slot_id") REFERENCES "cook_slots"("id") ON DELETE SET NULL ON UPDATE CASCADE;

