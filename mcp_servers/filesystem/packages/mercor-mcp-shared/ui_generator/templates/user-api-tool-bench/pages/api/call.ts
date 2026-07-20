// API route handler for proxying tool calls to MCP servers
import type { NextApiRequest, NextApiResponse } from 'next';
import axios from 'axios';
import { checkRateLimit } from '@/lib/rate-limit';
import { getDataTypeById, serverAuthConfig } from '@/lib/api-config';

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    const user = { email: 'user', userId: 'user' };

    if (!checkRateLimit(req, res)) {
      return;
    }

    const { dataTypeId, parameters } = req.body;

    if (!dataTypeId) {
      return res.status(400).json({ error: 'Data type ID is required' });
    }

    const dataType = getDataTypeById(dataTypeId);
    if (!dataType) {
      return res.status(404).json({ error: 'Data type not found' });
    }

    // Build request URL and parameters
    let url = dataType._internal.url;
    const queryParams: any = {};
    const bodyParams: any = {};

    if (parameters) {
      Object.entries(parameters).forEach(([key, value]) => {
        const param = dataType.parameters?.find(p => p.name === key);

        // Skip parameters not defined in API config
        if (!param) {
          console.warn(`Parameter '${key}' not found in API config for ${dataTypeId}, skipping`);
          return;
        }

        if (value !== '' && value !== null && value !== undefined) {
          let convertedValue: any = value;

          // Type conversion
          if (param.type === 'number') {
            convertedValue = parseFloat(String(value));
            if (isNaN(convertedValue)) {
              console.warn(`Invalid number for ${key}: ${value}, skipping`);
              return;
            }
          } else if (param.type === 'boolean') {
            convertedValue = String(value).toLowerCase() === 'true' || String(value) === '1';
          } else if (param.type === 'date' || param.type === 'datetime') {
            convertedValue = String(value);
          } else if (param.type === 'array') {
            // Split comma-separated values
            convertedValue = typeof value === 'string'
              ? value.split(',').map((v: string) => v.trim()).filter((v: string) => v)
              : value;
          } else if (param.type === 'object' && param.isJsonField) {
            // Parse JSON
            try {
              convertedValue = typeof value === 'string' ? JSON.parse(value) : value;
            } catch (e) {
              console.warn(`Invalid JSON for ${key}: ${value}, skipping`);
              return;
            }
          }

          // Check if parameter is in URL path
          const urlTemplate = `{${key}}`;
          if (url.includes(urlTemplate)) {
            url = url.replace(urlTemplate, encodeURIComponent(String(value)));
          } else if (param.location === 'query') {
            queryParams[key] = convertedValue;
          } else if (param.location === 'body') {
            bodyParams[key] = convertedValue;
          }
        }
      });
    }

    // Build headers
    const headers: any = {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };

    // Add authentication if required
    if (dataType._internal.requiresAuth) {
      const authConfig = serverAuthConfig[dataType.server];
      if (authConfig && authConfig.envVar) {
        // Access env var dynamically (works in API routes which run server-side)
        const token = process.env[authConfig.envVar];
        if (token) {
          headers['Authorization'] = `Bearer ${token}`;
        } else {
          console.warn(`Missing env var ${authConfig.envVar} for server ${dataType.server}`);
        }
      }
    }

    // Build request config
    const requestConfig: any = {
      method: dataType._internal.method,
      url: url,
      headers,
    };

    if (Object.keys(queryParams).length > 0) {
      requestConfig.params = queryParams;
    }
    if (Object.keys(bodyParams).length > 0) {
      requestConfig.data = bodyParams;
    }

    // Make the request
    const startTime = Date.now();
    const response = await axios(requestConfig);
    const duration = Date.now() - startTime;

    console.log(`[fetch] ${user.email} ${dataTypeId} ${duration}ms ${response.status}`);

    // Return response
    res.status(200).json({
      success: true,
      data: response.data,
      dataTypeName: dataType.name,
      server: dataType.server,
      metadata: {
        status: response.status,
        duration,
        timestamp: new Date().toISOString(),
      }
    });
  } catch (error: any) {
    console.error('fetch error:', error.message);

    if (error.response) {
      res.status(error.response.status).json({
        success: false,
        error: error.response.data || error.message,
        status: error.response.status,
      });
    } else {
      res.status(500).json({
        success: false,
        error: 'Failed to fetch data',
      });
    }
  }
}
