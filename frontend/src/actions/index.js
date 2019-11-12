import axios from 'axios';
import { MetricsBaseURL, S3Bucket, RoutesVersion, TripTimesVersion, WaitTimesVersion, ArrivalsVersion } from '../config';
import { getTimePath } from '../helpers/precomputed';

// S3 URL to route configuration
export function generateRoutesURL(agencyId) {
  return `https://${S3Bucket}.s3.amazonaws.com/routes/${RoutesVersion}/routes_${RoutesVersion}_${agencyId}.json.gz?b`;
}

/**
 * Generate S3 url for cached trip time statistics
 * @param agencyId {string} agency ID
 * @param dateStr {string} date
 * @param statPath {string} the statistical measure (e.g. median)
 * @param timePath {string} the time of day
 * @returns {string} S3 url
 */
export function generateTripTimesURL(agencyId, dateStr, statPath, timePath) {
  return `https://${S3Bucket}.s3.amazonaws.com/trip-times/${TripTimesVersion}/${agencyId}/${dateStr.replace(
    /-/g,
    '/',
  )}/trip-times_${TripTimesVersion}_${agencyId}_${dateStr}_${statPath}${timePath}.json.gz?e`;
}

/**
 * Generate S3 url for cached wait time statistics
 * @param agencyId {string} agency ID
 * @param dateStr {string} date
 * @param statPath {string} the statistical measure (e.g. median)
 * @param timePath {string} the time of day
 * @returns {string} S3 url
 */
export function generateWaitTimesURL(agencyId, dateStr, statPath, timePath) {
  return `https://${S3Bucket}.s3.amazonaws.com/wait-times/${WaitTimesVersion}/${agencyId}/${dateStr.replace(
    /-/g,
    '/',
  )}/wait-times_${WaitTimesVersion}_${agencyId}_${dateStr}_${statPath}${timePath}.json.gz?e`;
}

/**
 * Generate S3 url for arrivals
 * @param dateStr {string} date
 * @param routeId {string} route id
 * @returns {string} S3 url
 */
export function generateArrivalsURL(agencyId, dateStr, routeId) {
  return `https://${S3Bucket}.s3.amazonaws.com/arrivals/${ArrivalsVersion}/${agencyId}/${dateStr.replace(
    /-/g,
    '/',
  )}/arrivals_${ArrivalsVersion}_${agencyId}_${dateStr}_${routeId}.json.gz?d`;
}

export function fetchGraphData(params) {
  return function(dispatch) {

    var query = `query($agencyId:String!, $routeId:String!, $startStopId:String!, $endStopId:String,
    $directionId:String, $date:String!, $startTime:String, $endTime:String) {
  routeMetrics(agencyId:$agencyId, routeId:$routeId) {
    trip(startStopId:$startStopId, endStopId:$endStopId, directionId:$directionId) {
      interval(dates:[$date], startTime:$startTime, endTime:$endTime) {
        headways {
          count median max
          percentiles(percentiles:[90]) { percentile value }
          histogram { binStart binEnd count }
        }
        tripTimes {
          count median avg max
          percentiles(percentiles:[90]) { percentile value }
          histogram { binStart binEnd count }
        }
        waitTimes {
          median max
          percentiles(percentiles:[90]) { percentile value }
          histogram { binStart binEnd count }
        }
      }
      timeRanges(dates:[$date]) {
        startTime endTime
        waitTimes {
          percentiles(percentiles:[50,90]) { percentile value }
        }
        tripTimes {
          percentiles(percentiles:[50,90]) { percentile value }
        }
      }
    }
  }
}`.replace(/\s+/g, ' ');

    axios.get('/api/graphql', {
        params: { query: query, variables: JSON.stringify(params) },
        baseURL: MetricsBaseURL,
      })
      .then(response => {
        dispatch({
          type: 'RECEIVED_GRAPH_DATA',
          payload: response.data,
          graphParams: params,
        });
      })
      .catch(err => {
        const errStr =
          err.response && err.response.data && err.response.data.error
            ? err.response.data.error
            : err.message;
        dispatch({ type: 'RECEIVED_GRAPH_ERROR', payload: errStr });
      });
  };
}

export function resetGraphData() {
  return function(dispatch) {
    dispatch({ type: 'RESET_GRAPH_DATA', payload: null });
  };
}

export function fetchRoutes(params) {
  return function(dispatch) {
    const agencyId = params.agencyId;
    axios
      .get(generateRoutesURL(agencyId))
      .then(response => {
        var routes = response.data.routes;
        routes.forEach(route => {
          route.agencyId = agencyId;
        });
        dispatch({ type: 'RECEIVED_ROUTES', payload: routes });
      })
      .catch(err => {
        dispatch({ type: 'RECEIVED_ROUTES_ERROR', payload: err });
      });
  };
}

export function fetchPrecomputedWaitAndTripData(params) {
  return function(dispatch, getState) {
    const timeStr = params.startTime
      ? `${params.startTime}-${params.endTime}`
      : '';
    const dateStr = params.date;
    const agencyId = params.agencyId;

    const tripStatGroup = 'p10-median-p90'; // blocked; // 'median'
    const tripTimesCache = getState().routes.tripTimesCache;

    const tripTimesCacheKey = `${agencyId}-${dateStr + timeStr}-${tripStatGroup}`;

    const tripTimes = tripTimesCache[tripTimesCacheKey];

    if (!tripTimes) {
      const timePath = getTimePath(timeStr);
      const statPath = tripStatGroup;

      const s3Url = generateTripTimesURL(agencyId, dateStr, statPath, timePath);

      axios
        .get(s3Url)
        .then(response => {
          dispatch({
            type: 'RECEIVED_PRECOMPUTED_TRIP_TIMES',
            payload: [response.data, tripTimesCacheKey],
          });
        })
        .catch(() => {
          /* do something? */
        });
    }

    const waitStatGroup = 'median-p90-plt20m';
    const waitTimesCacheKey = `${agencyId}-${dateStr + timeStr}-${waitStatGroup}`;

    const waitTimesCache = getState().routes.waitTimesCache;
    const waitTimes = waitTimesCache[waitTimesCacheKey];

    if (!waitTimes) {
      const timePath = getTimePath(timeStr);
      const statPath = waitStatGroup; // for now, nothing clever about selecting smaller files here //getStatPath(statGroup);

      const s3Url = generateWaitTimesURL(agencyId, dateStr, statPath, timePath);

      axios
        .get(s3Url)
        .then(response => {
          dispatch({
            type: 'RECEIVED_PRECOMPUTED_WAIT_TIMES',
            payload: [response.data, waitTimesCacheKey],
          });
        })
        .catch(() => {
          /* do something? */
        });
    }
  };
}

/**
 * Action creator that fetches arrival history from S3 corresponding to the
 * day and route specified by params.
 *
 * @param params graphParams object
 */
export function fetchArrivals(params) {
  return function(dispatch) {
    const dateStr = params.date;
    const agencyId = params.agencyId;

    const s3Url = generateArrivalsURL(agencyId, dateStr, params.routeId);

    axios
      .get(s3Url)
      .then(response => {
        dispatch({
          type: 'RECEIVED_ARRIVALS',
          payload: [response.data, dateStr, params.routeId],
        });
      })
      .catch(err => {
        console.error(err);
      });
  };
}

export function handleSpiderMapClick(stops, latLng) {
  return function(dispatch) {
    dispatch({ type: 'RECEIVED_SPIDER_MAP_CLICK', payload: [stops, latLng] });
  };
}

export function handleGraphParams(params) {
  return function(dispatch, getState) {
    dispatch({ type: 'RECEIVED_GRAPH_PARAMS', payload: params });
    const graphParams = getState().routes.graphParams;

    // for debugging: console.log('hGP: ' + graphParams.routeId + ' dirid: ' + graphParams.directionId + " start: " + graphParams.startStopId + " end: " + graphParams.endStopId);
    // fetch graph data if all params provided
    // TODO: fetch route summary data if all we have is a route ID.

    if (graphParams.date && graphParams.agencyId) {
      dispatch(fetchPrecomputedWaitAndTripData(graphParams));
    }

    if (
      graphParams.agencyId &&
      graphParams.routeId &&
      graphParams.directionId &&
      graphParams.startStopId &&
      graphParams.endStopId
    ) {
      dispatch(fetchGraphData(graphParams));
    } else {
      // when we don't have all params, clear graph data

      dispatch(resetGraphData());
    }
  };
}
