# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
"""A BigQuery implementation of the cache."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, final

import google.oauth2
import sqlalchemy
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.oauth2 import service_account
from overrides import overrides

from airbyte import exceptions as exc
from airbyte._processors.file.jsonl import JsonlWriter
from airbyte._processors.sql.base import SqlProcessorBase
from airbyte.types import SQLTypeConverter


if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

    from airbyte._processors.file.base import FileWriterBase
    from airbyte.caches.base import CacheBase
    from airbyte.caches.bigquery import BigQueryCache


class BigQueryTypeConverter(SQLTypeConverter):
    """A class to convert types for BigQuery."""

    @classmethod
    def get_string_type(cls) -> sqlalchemy.types.TypeEngine:
        """Return the string type for BigQuery."""
        return "String"

    @overrides
    def to_sql_type(
        self,
        json_schema_property_def: dict[str, str | dict | list],
    ) -> sqlalchemy.types.TypeEngine:
        """Convert a value to a SQL type.

        We first call the parent class method to get the type. Then if the type is VARCHAR or
        BIGINT, we replace it with respective BigQuery types.
        """
        sql_type = super().to_sql_type(json_schema_property_def)
        # to-do: replace hardcoded return types with some sort of snowflake Variant equivalent
        if isinstance(sql_type, sqlalchemy.types.VARCHAR):
            return self.get_string_type()
        if isinstance(sql_type, sqlalchemy.types.BIGINT):
            return "INT64"

        return sql_type.__class__.__name__


class BigQuerySqlProcessor(SqlProcessorBase):
    """A BigQuery implementation of the cache."""

    file_writer_class = JsonlWriter
    type_converter_class = BigQueryTypeConverter
    supports_merge_insert = True

    cache: BigQueryCache

    def __init__(self, cache: CacheBase, file_writer: FileWriterBase | None = None) -> None:
        self._credentials: google.auth.credentials.Credentials | None = None
        self._schema_exists: bool | None = None
        super().__init__(cache, file_writer)

    @final
    @overrides
    def _fully_qualified(
        self,
        table_name: str,
    ) -> str:
        """Return the fully qualified name of the given table."""
        return f"`{self.cache.schema_name}`.`{table_name!s}`"

    @final
    @overrides
    def _quote_identifier(self, identifier: str) -> str:
        """Return the identifier name.

        BigQuery uses backticks to quote identifiers. Because BigQuery is case-sensitive for quoted
        identifiers, we convert the identifier to lowercase before quoting it.
        """
        return f"`{identifier.lower()}`"

    def _write_files_to_new_table(
        self,
        files: list[Path],
        stream_name: str,
        batch_id: str,
    ) -> str:
        """Write a file(s) to a new table.

        This is a generic implementation, which can be overridden by subclasses
        to improve performance.
        """
        temp_table_name = self._create_table_for_loading(stream_name, batch_id)

        # Specify the table ID (in the format `project_id.dataset_id.table_id`)
        table_id = f"{self.cache.project_name}.{self.cache.dataset_name}.{temp_table_name}"

        # Initialize a BigQuery client
        client = bigquery.Client(credentials=self._get_credentials())

        for file_path in files:
            with Path.open(file_path, "rb") as source_file:
                load_job = client.load_table_from_file(  # Make an API request
                    file_obj=source_file,
                    destination=table_id,
                    job_config=bigquery.LoadJobConfig(
                        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                        schema=[
                            bigquery.SchemaField(name, field_type=str(type_))
                            for name, type_ in self._get_sql_column_definitions(
                                stream_name=stream_name
                            ).items()
                        ],
                    ),
                )
            _ = load_job.result()  # Wait for the job to complete

        return temp_table_name

    def _ensure_schema_exists(
        self,
    ) -> None:
        """Ensure the target schema exists.

        We override the default implementation because BigQuery is very slow at scanning schemas.

        This implementation simply calls "CREATE SCHEMA IF NOT EXISTS" and ignores any errors.
        """
        if self._schema_exists:
            return

        sql = f"CREATE SCHEMA IF NOT EXISTS {self.cache.schema_name}"
        try:
            self._execute_sql(sql)
        except Exception as ex:
            # Ignore schema exists errors.
            if "already exists" not in str(ex):
                raise

        self._schema_exists = True

    def _get_credentials(self) -> google.auth.credentials.Credentials:
        """Return the GCP credentials."""
        if self._credentials is None:
            if self.cache.credentials_path:
                self._credentials = service_account.Credentials.from_service_account_file(
                    self.cache.credentials_path
                )
            else:
                self._credentials, _ = google.auth.default()
        return self._credentials

    def _table_exists(
        self,
        table_name: str,
    ) -> bool:
        """Return true if the given table exists.

        We override the default implementation because BigQuery is very slow at scanning tables.
        """
        client = bigquery.Client(credentials=self._get_credentials())
        table_id = f"{self.cache.project_name}.{self.cache.dataset_name}.{table_name}"
        try:
            client.get_table(table_id)
        except NotFound:
            return False

        except ValueError as ex:
            raise exc.PyAirbyteInputError(
                message="Invalid project name or dataset name.",
                context={
                    "table_id": table_id,
                    "table_name": table_name,
                    "project_name": self.cache.project_name,
                    "dataset_name": self.cache.dataset_name,
                },
            ) from ex

        return True

    @final
    @overrides
    def _get_tables_list(
        self,
    ) -> list[str]:
        """Get the list of available tables in the schema.

        For bigquery, {schema_name}.{table_name} is returned, so we need to
        strip the schema name in front of the table name, if it exists.

        Warning: This method is slow for BigQuery, as it needs to scan all tables in the dataset.
        It has been observed to take 30+ seconds in some cases.
        """
        with self.get_sql_connection() as conn:
            inspector: Inspector = sqlalchemy.inspect(conn)
            tables = inspector.get_table_names(schema=self.cache.schema_name)
            schema_prefix = f"{self.cache.schema_name}."
            return [
                table.replace(schema_prefix, "", 1) if table.startswith(schema_prefix) else table
                for table in tables
            ]

    def _swap_temp_table_with_final_table(
        self,
        stream_name: str,
        temp_table_name: str,
        final_table_name: str,
    ) -> None:
        """Swap the temp table with the main one, dropping the old version of the 'final' table.

        The BigQuery RENAME implementation requires that the table schema (dataset) is named in the
        first part of the ALTER statement, but not in the second part.

        For example, BigQuery expects this format:

        ALTER TABLE my_schema.my_old_table_name RENAME TO my_new_table_name;
        """
        if final_table_name is None:
            raise exc.PyAirbyteInternalError(message="Arg 'final_table_name' cannot be None.")
        if temp_table_name is None:
            raise exc.PyAirbyteInternalError(message="Arg 'temp_table_name' cannot be None.")

        _ = stream_name
        deletion_name = f"{final_table_name}_deleteme"
        commands = "\n".join(
            [
                f"ALTER TABLE {self._fully_qualified(final_table_name)} "
                f"RENAME TO {deletion_name};",
                f"ALTER TABLE {self._fully_qualified(temp_table_name)} "
                f"RENAME TO {final_table_name};",
                f"DROP TABLE {self._fully_qualified(deletion_name)};",
            ]
        )
        self._execute_sql(commands)
