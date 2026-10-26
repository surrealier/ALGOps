"""v2 baseline schema

Revision ID: 0001_v2_baseline
Revises: 
Create Date: 2026-10-26 19:23:44.444632
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0001_v2_baseline'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('benchmark',
    sa.Column('benchmark_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=True),
    sa.Column('baseline', sa.String(length=64), nullable=False),
    sa.Column('revisions', sa.JSON(), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('schedule_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('benchmark_id')
    )
    op.create_table('project',
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('modality', sa.String(length=32), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('param_spec', sa.JSON(), nullable=True),
    sa.Column('summary_md_uri', sa.String(length=512), nullable=True),
    sa.Column('repo_kind', sa.String(length=16), nullable=False),
    sa.Column('repo_url', sa.String(length=1024), nullable=True),
    sa.Column('default_ref', sa.String(length=255), nullable=True),
    sa.Column('watch', sa.Boolean(), nullable=False),
    sa.Column('last_seen_sha', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('project_id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('schedule',
    sa.Column('schedule_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('cron', sa.String(length=128), nullable=True),
    sa.Column('interval_s', sa.Integer(), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_benchmark_id', sa.String(length=36), nullable=True),
    sa.Column('last_seen', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('schedule_id')
    )
    op.create_table('slave',
    sa.Column('slave_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('host_label', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_heartbeat', sa.DateTime(timezone=True), nullable=True),
    sa.Column('labels', sa.JSON(), nullable=True),
    sa.Column('concurrency', sa.Integer(), nullable=False),
    sa.Column('inflight', sa.Integer(), nullable=False),
    sa.Column('git', sa.Boolean(), nullable=False),
    sa.Column('allow_projects', sa.JSON(), nullable=True),
    sa.Column('local_projects', sa.JSON(), nullable=True),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('slave_id')
    )
    op.create_table('benchmark_cell',
    sa.Column('cell_id', sa.String(length=36), nullable=False),
    sa.Column('benchmark_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('algorithm_id', sa.String(length=64), nullable=False),
    sa.Column('suite_id', sa.String(length=64), nullable=False),
    sa.Column('commit_sha', sa.String(length=80), nullable=True),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('verdict', sa.String(length=16), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=True),
    sa.Column('gates', sa.JSON(), nullable=True),
    sa.Column('baseline_run_id', sa.String(length=36), nullable=True),
    sa.Column('baseline_sha', sa.String(length=80), nullable=True),
    sa.Column('deltas', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('duration_s', sa.Float(), nullable=True),
    sa.Column('accepted', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['benchmark_id'], ['benchmark.benchmark_id'], ),
    sa.PrimaryKeyConstraint('cell_id')
    )
    op.create_index('ix_cell_key', 'benchmark_cell', ['project_id', 'algorithm_id', 'suite_id'], unique=False)
    op.create_table('experiment',
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('goal_text', sa.Text(), nullable=False),
    sa.Column('target_metric', sa.String(length=128), nullable=False),
    sa.Column('target_value', sa.Float(), nullable=True),
    sa.Column('goal_direction', sa.String(length=16), nullable=False),
    sa.Column('max_iteration', sa.Integer(), nullable=False),
    sa.Column('primary_param_key', sa.String(length=255), nullable=True),
    sa.Column('test_set_uri', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('best_run_id', sa.String(length=36), nullable=True),
    sa.Column('langgraph_thread_id', sa.String(length=80), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('llm_config', sa.JSON(), nullable=True),
    sa.Column('algorithm_id', sa.String(length=64), nullable=False),
    sa.Column('suite_id', sa.String(length=64), nullable=False),
    sa.Column('validation_suite_ids', sa.JSON(), nullable=True),
    sa.Column('objective', sa.JSON(), nullable=True),
    sa.Column('strategy', sa.String(length=32), nullable=False),
    sa.Column('search_keys', sa.JSON(), nullable=True),
    sa.Column('batch_size', sa.Integer(), nullable=False),
    sa.Column('seed', sa.Integer(), nullable=False),
    sa.Column('ref', sa.String(length=255), nullable=True),
    sa.Column('commit_sha', sa.String(length=80), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.project_id'], ),
    sa.PrimaryKeyConstraint('experiment_id')
    )
    op.create_table('project_revision',
    sa.Column('revision_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('ref', sa.String(length=255), nullable=True),
    sa.Column('sha', sa.String(length=80), nullable=False),
    sa.Column('dirty', sa.Boolean(), nullable=False),
    sa.Column('manifest', sa.JSON(), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.project_id'], ),
    sa.PrimaryKeyConstraint('revision_id')
    )
    op.create_index('ix_revision_project_synced', 'project_revision', ['project_id', 'synced_at'], unique=False)
    op.create_table('final_result',
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('winning_run_id', sa.String(length=36), nullable=True),
    sa.Column('optimal_params', sa.JSON(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=True),
    sa.Column('stop_reason', sa.String(length=32), nullable=True),
    sa.Column('report_md_uri', sa.String(length=512), nullable=True),
    sa.Column('notified', sa.Boolean(), nullable=False),
    sa.Column('notify_refs', sa.JSON(), nullable=True),
    sa.Column('validation', sa.JSON(), nullable=True),
    sa.Column('changed_params', sa.JSON(), nullable=True),
    sa.Column('promotion', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['experiment_id'], ['experiment.experiment_id'], ),
    sa.PrimaryKeyConstraint('experiment_id')
    )
    op.create_table('iteration',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('iteration_index', sa.Integer(), nullable=False),
    sa.Column('analyst_findings', sa.Text(), nullable=True),
    sa.Column('pm_action', sa.String(length=16), nullable=True),
    sa.Column('stop_reason', sa.String(length=32), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('proposed_run_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['experiment_id'], ['experiment.experiment_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('run',
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=True),
    sa.Column('benchmark_id', sa.String(length=36), nullable=True),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('algorithm_id', sa.String(length=64), nullable=False),
    sa.Column('suite_id', sa.String(length=64), nullable=False),
    sa.Column('commit_sha', sa.String(length=80), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('iteration_index', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('param_values', sa.JSON(), nullable=False),
    sa.Column('derived_from_run', sa.String(length=36), nullable=True),
    sa.Column('metrics', sa.JSON(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('n_items', sa.Integer(), nullable=True),
    sa.Column('csv_manifest_uri', sa.String(length=512), nullable=True),
    sa.Column('summary_md_uri', sa.String(length=512), nullable=True),
    sa.Column('log_uri', sa.String(length=512), nullable=True),
    sa.Column('slave_id', sa.String(length=36), nullable=True),
    sa.Column('duration_s', sa.Float(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_kind', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['experiment_id'], ['experiment.experiment_id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_index('ix_run_benchmark', 'run', ['benchmark_id'], unique=False)
    op.create_index('ix_run_cell', 'run', ['project_id', 'algorithm_id', 'suite_id', 'created_at'], unique=False)
    op.create_index('ix_run_experiment', 'run', ['experiment_id', 'attempt', 'iteration_index'], unique=False)
    op.create_table('run_metric',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['run.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('run_param',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('value', sa.String(length=250), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['run.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('run_param')
    op.drop_table('run_metric')
    op.drop_index('ix_run_experiment', table_name='run')
    op.drop_index('ix_run_cell', table_name='run')
    op.drop_index('ix_run_benchmark', table_name='run')
    op.drop_table('run')
    op.drop_table('iteration')
    op.drop_table('final_result')
    op.drop_index('ix_revision_project_synced', table_name='project_revision')
    op.drop_table('project_revision')
    op.drop_table('experiment')
    op.drop_index('ix_cell_key', table_name='benchmark_cell')
    op.drop_table('benchmark_cell')
    op.drop_table('slave')
    op.drop_table('schedule')
    op.drop_table('project')
    op.drop_table('benchmark')
