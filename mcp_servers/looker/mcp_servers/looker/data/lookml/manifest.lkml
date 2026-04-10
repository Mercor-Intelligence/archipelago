project_name: "mercor_seeded_data"

# This project contains auto-generated LookML from CSV data files.
# See docs/LOOKML_DEPLOYMENT.md for details.

# Configure these constants based on your database setup
constant: database_schema {
  value: "public"
  export: override_optional
}

constant: database_connection {
  value: "mercor"
  export: override_optional
}
